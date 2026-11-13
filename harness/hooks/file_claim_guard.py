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
`tee`, `sed -i`. Не видно: запись изнутри скриптов, cp/mv/git, пути в переменных
и подстановках. Исключение — исключительный путь в теле here-doc или аргументе
`python3 -c`: он считается записью, если рядом есть признак записи (write_text,
write_bytes, open(…, 'w'|'a'), .write(, yaml.dump, json.dump, shutil.copy,
os.replace); чтение (cat, grep, yaml.safe_load, read_text) записью не считается.

Хук не роняет запись никогда: любая своя ошибка — код 0 и строка skipped.
Заявлять имеет смысл только пути дерева проекта и пакета: черновики в /tmp
журнал не засоряют.

Исключительные пути (FILE_CLAIM_EXCLUSIVE, от корня дерева; тот же путь есть и в
проекте, и в пакете — это один замок) не предупреждают, а отказывают кодом 2:
package_sync сверяет файл пакета с индексом проекта ЦЕЛИКОМ, и две задачи с
ханками в одном общем конфиге не могут закоммитить его по отдельности. Держатель —
задача: у помощника метка «Часть:» его запуска (meta.json → toolUseId → строка
agent.launch → launch_plan_lib.mark_task), у главной сессии — «смена». Замок
снимается сам, когда путь совпал с HEAD и в проекте, и в пакете; по времени он не
снимается, «брошено» только называется иначе. Законная дверь на один путь —
`--door <путь> 'причина'` на 15 минут. Своя ошибка хука по-прежнему код 0: отказ
бывает только по доказанному занятию.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import functools
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import config                                            # noqa: E402
import events                                            # noqa: E402
import launch_plan_lib as plan                           # noqa: E402

# Имя журнала стоит литералом и в emit(): ворота G2 узнают писателя по нему.
JOURNAL = "заявки-на-файлы"
SRC = "scripts/hooks/file_claim_guard.py"
CLAIM_EVENT = "file.claim"
WARN_EVENT = "file.claim.warn"
REPEAT_EVENT = "file.claim.repeat"
REMIND_EVENT = "file.claim.remind"
WINDOW_KEY = "FILE_CLAIM_MIN"
SHARED_KEY = "FILE_CLAIM_SHARED"
EXCLUSIVE_KEY = "FILE_CLAIM_EXCLUSIVE"
REFUSED_EVENT = "file.claim.refused"
DOOR_EVENT = "file.claim.door"
DOOR_NAME = "file-claim.door"
LOCK_NAME = "file-claim.lock"
DOOR_TTL_S = 15 * 60
DOOR_REASON_MIN = 10
SHIFT = "смена"
NO_AGENT = "-"
LAUNCH_JOURNAL = "вызовы-агентов"
LAUNCH_EVENT = "agent.launch"
GIT_TIMEOUT_S = 3
# Окно в час — сотни заявок; хвоста в 1 МБ хватает с запасом, а журнал за
# месяц читать на каждой записи дорого.
TAIL_BYTES = 1 << 20
PATH_FIELD = {"Write": "file_path", "Edit": "file_path", "NotebookEdit": "notebook_path"}
REDIRECTS = (">", ">>", ">|", "&>", "&>>")
OPERATOR_CHARS = set("();<>|&")
UNEXPANDED = set("$`*?~")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
WRITE_MARK = re.compile(r"\.write_text\(|\.write_bytes\(|\.write\(|\bopen\([^)]*['\"][wax]\+?b?['\"]"
                        r"|yaml\.(?:safe_)?dump|json\.dump|shutil\.copy|os\.replace")


@functools.lru_cache(maxsize=None)
def setting(name: str) -> str:
    """Ключ установленного harness.conf, иначе — конфига дерева."""
    tree_conf = config.корень_дерева(__file__) / "harness" / "config" / "harness.conf"
    return (config.из_файла(config.путь_harness_conf()).get(name)
            or config.из_файла(str(tree_conf)).get(name) or "")


def window_seconds() -> float:
    return float(setting(WINDOW_KEY)) * 60


def journal_path() -> Path:
    return Path(events.log_dir()) / f"{JOURNAL}.jsonl"


def record(event: str, outcome: str, session: str, data: dict, task: str = "-") -> None:
    # Задачу заявки знает только holder_of (метка запуска): единственная wip карта
    # или трейлер истории называли бы чужую.
    events.emit(SRC, event, outcome, journal="заявки-на-файлы", task=task,
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


def heredoc_bodies(command: str) -> str:
    """Тела here-doc одним текстом: скрипт в теле может писать, хотя `>` в нём не пишет."""
    bodies, ends = [], []
    for line in command.split("\n"):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            else:
                bodies.append(line)
            continue
        ends += [m.group(2) for m in HEREDOC.finditer(line)]
    return "\n".join(bodies)


def script_targets(command: str) -> list:
    """Исключительные пути, названные в скрипте (here-doc или `python3 -c`) вместе с
    признаком записи: путь от корня проекта — замок один для проекта и пакета."""
    lexer = shlex.shlex(strip_heredocs(command).replace("\n", " ; "), posix=True,
                        punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = []
    bodies = [heredoc_bodies(command)] + [
        tokens[i + 1] for i in range(1, len(tokens) - 1) if tokens[i] == "-c"
        and any(os.path.basename(t).startswith("python") for t in tokens[max(0, i - 4):i])]
    root = str(config.корень_дерева(__file__))
    return [os.path.join(root, rel) for rel in setting(EXCLUSIVE_KEY).split()
            if any(rel in body and WRITE_MARK.search(body) for body in bodies)]


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
        command = str(args.get("command") or "")
        raw = bash_targets(command) + script_targets(command)
    else:
        return []
    cwd = str(payload.get("cwd") or os.getcwd())
    roots = [str(config.корень_дерева(__file__)), str(config.katalog_paketa())]
    paths = [os.path.normpath(os.path.join(cwd, p)) for p in raw if p]
    return [p for p in dict.fromkeys(paths)
            if any(p.startswith(root.rstrip("/") + "/") for root in roots)]


def recent_rows(now: float, window_s: float | None) -> list:
    """Строки хука за окно (None — весь хвост); к строке добавлены время `_at` и
    возраст `_age` в секундах."""
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
            at = dt.datetime.fromisoformat(row["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        age = now - at
        if isinstance(row, dict) and row.get("src") == SRC and (window_s is None or age <= window_s):
            row["_at"], row["_age"] = at, age
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


def inside_task_card(path: str, task: str) -> bool:
    """Путь в .tasks/<task>/ проекта или пакета: карточку задачи пишут её исполнители."""
    return any(path.startswith(f"{str(root).rstrip('/')}/.tasks/{task}/") for root in tree_roots())


def is_real_intersection(row: dict, path: str, who: dict) -> bool:
    """Чужая заявка мешает, если держатель — другая задача и путь не в карточке пишущего.
    «агент:<id>» — задача неизвестна, это пересечение."""
    return row.get("task") != who["task"] and not inside_task_card(path, who["task"])


def warn_others(paths: list, payload: dict) -> str:
    """Предупреждения о чужих заявках на обычные и общие пути; заявки — строками журнала."""
    agent, session = str(payload["agent_id"]), str(payload.get("session_id") or "")
    window_s = window_seconds()
    shared = setting(SHARED_KEY).split()
    rows = recent_rows(time.time(), window_s)
    claims = holders(rows)
    who = holder_of(payload)
    texts = []
    for path in paths:
        others = {a: r for a, r in claims.get(path, {}).items()
                  if a != agent and is_real_intersection(r, path, who)}
        mine = claims.get(path, {}).get(agent)
        if mine is None or mine["_age"] > window_s / 2:
            record(CLAIM_EVENT, "ok", session, {"путь": path, "агент": agent,
                                                 "тип": payload.get("agent_type") or None},
                   task=who["task"])
        is_shared = any(path.endswith("/" + rel.strip("/")) for rel in shared)
        if not others and not (is_shared and mine is None):
            continue
        earlier = sum(1 for r in rows if r.get("event") in (WARN_EVENT, REPEAT_EVENT, REMIND_EVENT)
                      and (r.get("data") or {}).get("путь") == path
                      and (r.get("data") or {}).get("агент") == agent)
        number = 1 + earlier
        # Только первое предупреждение пары за окно — сигнал пересечения; повтор и
        # напоминание без держателя судью скорости не питают.
        event = REMIND_EVENT if not others else REPEAT_EVENT if earlier else WARN_EVENT
        record(event, "ok", session, {"путь": path, "агент": agent, "общий": is_shared,
                                       "занявшие": sorted(others), "номер": number},
               task=who["task"])
        texts.append(warning(path, others, is_shared, number))
    return "\n\n".join(texts)


# ───────────────────────── исключительный замок ─────────────────────────

@functools.lru_cache(maxsize=1)
def tree_roots() -> tuple:
    return config.корень_дерева(__file__), config.katalog_paketa()


def tree_rel(path: str) -> str | None:
    """Путь от корня проекта или пакета: ключ замка один для обоих деревьев."""
    for root in tree_roots():
        prefix = str(root).rstrip("/") + "/"
        if path.startswith(prefix):
            return path[len(prefix):]
    return None


def exclusive_rel(path: str) -> str | None:
    """Путь от корня дерева, если он в FILE_CLAIM_EXCLUSIVE."""
    rel = tree_rel(path)
    return rel if rel in setting(EXCLUSIVE_KEY).split() else None


def git(root, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_S)


def in_head(root, rel: str) -> bool:
    return git(root, "cat-file", "-e", f"HEAD:{rel}").returncode == 0


def lock_state(rel: str) -> tuple[bool, float, float] | None:
    """(грязно ли в проекте или в пакете, граница = старший из двух коммитов пути,
    предпоследний коммит пути: до него заявка — прошлый эпизод, а не хвост).

    None — пути нет в HEAD проекта: `git diff HEAD` не видит неотслеживаемый файл и
    отвечал бы «чист», замок такому пути не годится. Пакета или файла в нём нет —
    судим по одному проекту."""
    project, package = tree_roots()
    if not in_head(project, rel):
        return None
    sides = [project] + ([package] if in_head(package, rel) else [])
    dirty = False
    for root in sides:
        code = git(root, "diff", "--quiet", "HEAD", "--", rel).returncode
        if code not in (0, 1):
            raise RuntimeError(f"git diff в {root}: код {code}")
        dirty = dirty or code == 1
    if not dirty:
        return False, 0.0, 0.0
    commits = [git(root, "log", "-2", "--format=%ct", "--", rel).stdout.split() for root in sides]
    return (True, min(float(c[0]) for c in commits),
            min(float(c[1]) if len(c) > 1 else 0.0 for c in commits))


def launch_index() -> dict:
    """Вызов (tool_use_id) → первая строка запуска помощника с ним."""
    index: dict = {}
    rows = plan.read_rows(Path(events.log_dir()) / f"{LAUNCH_JOURNAL}.jsonl")
    for row in rows:
        data = row.get("data")
        call = data.get("вызов") if isinstance(data, dict) else None
        if row.get("event") == LAUNCH_EVENT and call:
            index.setdefault(call, row)
    return index


def launch_task(payload: dict, agent: str) -> str | None:
    """Задача помощника по метке «Часть:» его запуска, иначе — запуска родителя."""
    call = plan.tool_use_id_of(payload, agent)
    if not call:
        if plan.meta_path(payload, agent).exists():
            raise ValueError("meta.json без toolUseId")
        return None
    index = launch_index()
    row = index.get(call)
    if row is None:
        return None
    return plan.mark_task(row) or plan.mark_task(
        index.get((row.get("data") or {}).get("родитель")) or {})


def holder_of(payload: dict) -> dict:
    """Кто пишет: задача, ключ агента в заявках и тип. Смена — своя «задача»."""
    agent = str(payload.get("agent_id") or "")
    if not agent:
        return {"task": SHIFT, "agent": NO_AGENT, "type": None}
    return {"task": launch_task(payload, agent) or f"агент:{agent}", "agent": agent,
            "type": payload.get("agent_type") or None}


def is_mine(row: dict, who: dict) -> bool:
    """Своя заявка — той же задачи или того же агента: старые строки (task="-")
    не должны запирать собственного держателя."""
    return row.get("task") == who["task"] or (row.get("data") or {}).get("агент") == who["agent"]


def door_reason(rel: str) -> str:
    try:
        door = json.loads((Path(events.log_dir()) / DOOR_NAME).read_text(encoding="utf-8"))
        if door.get("путь") == rel and float(door.get("до") or 0) >= time.time():
            return str(door.get("почему") or "").strip()
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return ""


def refusal_kind(claims: list, who: dict, bounds: tuple, window_s: float):
    """(вид отказа или "", заявки держателя). claims — заявки на путь, без дверных;
    bounds — (граница, предпоследний коммит пути)."""
    boundary, prev = bounds
    live = [r for r in claims if r["_at"] > boundary]
    if not live:
        # Граница отсекла всё: ханки либо свои после частичного коммита (своя последняя
        # заявка — после предпоследнего коммита), либо ничьи.
        own_tail = claims and is_mine(claims[-1], who) and claims[-1]["_at"] > prev
        return ("" if own_tail else "без-заявки"), []
    others = [r for r in live if not is_mine(r, who)]
    if not others:
        return "", []
    holder = others[-1].get("task")
    kind = "занято" if min(r["_age"] for r in others) < window_s else "брошено"
    return kind, [r for r in others if r.get("task") == holder]


def holder_summary(held: list) -> dict:
    data = held[-1].get("data") or {}
    return {"задача": held[-1].get("task"), "агент": data.get("агент"), "тип": data.get("тип"),
            "с": dt.datetime.fromtimestamp(held[0]["_at"]).strftime("%H:%M"),
            "мин_назад": int(held[-1]["_age"] // 60)}


def refusal_text(rel: str, kind: str, held: dict | None) -> str:
    if held:
        agent = SHIFT if held["агент"] == NO_AGENT else f"помощник {held['агент'][:8]}"
        who = f"{agent}, {held['тип'] or 'тип не назван'}, с {held['с']}"
        state = (f"последняя запись {held['мин_назад']} мин назад" if kind == "занято" else
                 f"молчит {held['мин_назад']} мин — ханки брошены, решение за оркестратором")
        head = f"{rel} держит задача {held['задача']} ({who}, {state})."
    else:
        head = (f"{rel}: ханки записаны мимо хука, держатель неизвестен; если они твои — "
                f"дверь с причиной.")
    return (f"{head} Общий конфиг правит одна задача за раз: её ханки не закоммичены, а "
            f"package_sync сверяет файл целиком.\n"
            f"Что делать: дождаться её коммита (замок снимется сам, когда файл и пакетная "
            f"пара совпадут с HEAD) или договориться через оркестратора. Кто что держит: "
            f"python3 scripts/who-writes.py. Законная дверь на {DOOR_TTL_S // 60} минут:\n"
            f"python3 scripts/hooks/file_claim_guard.py --door {rel} 'причина'\n"
            f"Дверь на чужие ханки не открывай — это снова общий коммит; верни управляющему "
            f"вариант: ждать, взять другую часть, попросить смену закоммитить за держателя.")


def judge_exclusive(paths: dict, states: dict, who: dict, session: str) -> tuple[str, int]:
    """Решение по исключительным путям {путь: rel}: (текст, код). Отказ по любому пути
    отказывает команду целиком и заявок не пишет — отказанный держателем не становится."""
    window_s = window_seconds()
    rows = [r for r in recent_rows(time.time(), None)
            if r.get("event") == CLAIM_EVENT and r.get("outcome") == "ok"
            and not (r.get("data") or {}).get("дверь")]
    refusals, grants, reminders = [], [], []
    for path, rel in paths.items():
        dirty, boundary, prev = states[path]
        claims = [r for r in rows if tree_rel((r.get("data") or {}).get("путь") or "") == rel]
        kind, held = refusal_kind(claims, who, (boundary, prev), window_s) if dirty else ("", [])
        reason = door_reason(rel) if kind else ""
        if kind and not reason:
            refusals.append((path, rel, kind, held))
            continue
        mine_live = [r for r in claims if dirty and r["_at"] > boundary and is_mine(r, who)]
        if reason or not mine_live or mine_live[-1]["_age"] > window_s / 2:
            grants.append((path, reason))
        if not reason and not mine_live:
            reminders.append(rel)
    base = {"агент": who["agent"], "тип": who["type"]}
    for path, rel, kind, held in refusals:
        summary = holder_summary(held) if held else None
        record(REFUSED_EVENT, "refused", session,
               {"путь": path, "вид": kind, "держатель": summary, **base}, task=who["task"])
    if refusals:
        return "\n\n".join(refusal_text(rel, kind, holder_summary(held) if held else None)
                           for _, rel, kind, held in refusals), 2
    for path, reason in grants:
        record(CLAIM_EVENT, "ok", session,
               {"путь": path, **base, **({"дверь": reason} if reason else {})}, task=who["task"])
    return "".join(
        f"Ты держишь общий конфиг {rel}: закоммить свои строки отдельным коммитом сразу "
        f"(проект и пакет), другие задачи ждут твоего коммита.\n" for rel in reminders).strip(), 0


def decide(payload: dict) -> tuple[str, int]:
    """(текст, код): код 2 — отказ, текст идёт в stderr; иначе текст — additionalContext."""
    agent = str(payload.get("agent_id") or "")
    session = str(payload.get("session_id") or "")
    paths = targets(payload)
    exclusive = {p: rel for p in paths if (rel := exclusive_rel(p))}
    plain = [p for p in paths if p not in exclusive]
    texts = []
    if exclusive:
        lock = Path(events.log_dir()) / LOCK_NAME
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            states = {p: lock_state(rel) for p, rel in exclusive.items()}
            for path in [p for p, st in states.items() if st is None]:
                record(CLAIM_EVENT, "skipped", session,
                       {"путь": path, "почему": "путь не в HEAD проекта: замок не годится"})
                exclusive.pop(path)
            if exclusive:
                text, code = judge_exclusive(exclusive, states, holder_of(payload), session)
                if code:
                    return text, code
                texts.append(text)
    if agent and plain:
        texts.append(warn_others(plain, payload))
    return "\n\n".join(t for t in texts if t), 0


def open_door(path: str, reason: str) -> int:
    reason = reason.strip()
    if len(reason) < DOOR_REASON_MIN:
        print(f"дверь не открыта: причина короче {DOOR_REASON_MIN} знаков", file=sys.stderr)
        return 1
    absolute = os.path.normpath(os.path.join(os.getcwd(), path))
    rel = exclusive_rel(absolute)
    if rel is None:
        print(f"дверь не открыта: {path} не в {EXCLUSIVE_KEY}", file=sys.stderr)
        return 1
    until = int(time.time()) + DOOR_TTL_S
    door = Path(events.log_dir()) / DOOR_NAME
    door.parent.mkdir(parents=True, exist_ok=True)
    door.write_text(json.dumps({"путь": rel, "почему": reason, "до": until},
                               ensure_ascii=False), encoding="utf-8")
    record(DOOR_EVENT, "ok", "", {"путь": rel, "почему": reason, "до": until})
    print(f"дверь на {rel} открыта на {DOOR_TTL_S // 60} минут: {reason}")
    return 0


def main() -> int:
    if "--door" in sys.argv:
        args = sys.argv[sys.argv.index("--door") + 1:]
        return open_door(args[0] if args else "", args[1] if len(args) > 1 else "")
    try:
        payload = json.load(sys.stdin)
        text, code = decide(payload) if isinstance(payload, dict) else ("", 0)
    except Exception as exc:  # noqa: BLE001 — своя ошибка хука не роняет запись
        record(CLAIM_EVENT, "skipped", "", {"почему": f"{type(exc).__name__}: {exc}"[:300]})
        return 0
    if code:
        print(text, file=sys.stderr)
    elif text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": text}},
                         ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
