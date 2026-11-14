"""История пакета для раскладки: какие файлы проекта положил сам раскладчик.

Файл, которого нет в описи, но чей путь И содержимое встречались в истории
пакета, — наш (устаревший); правка в один байт делает его чужим. Модуль судит: файлы снимает дверь, вопрос
владельцу отправляет update.py.
Пишет в LOG_DIR только по зову update.py: журнал вопроса и итог последнего прогона.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import layout_journal  # noqa: E402
import layout_manifest  # noqa: E402
import owner_word  # noqa: E402

EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
ZERO_BLOB = "0" * 40
EMPTY = "EMPTY"
ASKED_LOG = "раскладка-спрошено.jsonl"
SEED_VIEW_LOG = "раскладка-затравка.json"
CONSENT_LOG = "confirmations.jsonl"
CONSENT_MAX_AGE_DAYS = 7

# HEAD и теги, а не --branches/--all: история пакета — то, что достижимо из
# коммита, на котором стоит пакет, и из релизных тегов. Ветка «страховка»
# (`update.py --сбросить-свои` кладёт в неё свои коммиты копии) недостижима
# из HEAD, и её файлы не становятся «нашими»; --all тянет ещё refs/remotes/* и
# stash. -z и core.quotepath=off: путь приходит дословно, без кавычек и
# восьмеричных escape для кириллицы. --no-abbrev: сверка идёт по полному blob.
# --no-renames: переименование иначе скрыло бы старый путь.
LOG_ARGS = ("-c", "core.quotepath=off", "log", "HEAD", "--tags", "--no-renames",
            "--raw", "--no-abbrev", "-z", "--format=%x01%H")

# \b слеп к кириллице, поэтому границы слова берутся классом букв.
_WORD_YES = re.compile(r"(?<![^\W\d_])да(?![^\W\d_])")
_WORD_REMOVE = re.compile(r"(?<![^\W\d_])убрать(?![^\W\d_])")


@dataclass
class Rules:
    translate: dict
    outside: dict


@dataclass
class History:
    blobs: dict = field(default_factory=dict)
    outside: dict = field(default_factory=dict)
    shallow: bool = False
    commits: int = 0


@dataclass
class NoHistory:
    reason: str


@dataclass
class Unreadable:
    reason: str


@dataclass
class Question:
    ts: dt.datetime
    paths: list


@dataclass
class Seed:
    would_remove: dict
    foreign: list
    strangers: int
    empty: int
    outside: int


def load_rules(path) -> Rules:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Rules(dict(data.get("перевод") or {}), dict(data.get("вне_корней") or {}))


def _matches(pkg_path: str, key: str) -> bool:
    return pkg_path.startswith(key) if key.endswith("/") else pkg_path == key


def _place(pkg_path: str, rules: Rules):
    """(вид, путь проекта) по самому длинному ключу обеих таблиц; None — не кладётся."""
    best = None
    for table, kind in ((rules.translate, "in"), (rules.outside, "out")):
        for key, value in table.items():
            if _matches(pkg_path, key) and (best is None or len(key) > len(best[0])):
                best = (key, value, kind)
    if best is None or best[1] is None:
        return None
    key, value, kind = best
    return kind, (value + pkg_path[len(key):] if key.endswith("/") else value)


def project_path(pkg_path: str, rules: Rules):
    place = _place(pkg_path, rules)
    return place[1] if place and place[0] == "in" else None


def _git(package_root, *args):
    return subprocess.run(["git", "-C", str(package_root), *args], capture_output=True)


# Своё копии: коммиты локальных веток, которых нет ни в HEAD, ни в тегах, ни в remote-ветках
# (после `update.py --сбросить-свои` они лежат в ветке-страховке). Имя ветки не судится.
# HEAD не берётся: на мастерской источника он держит незапушенное, и тогда не снялось бы
# ничего удалённое.
OWN_ARGS = ("-c", "core.quotepath=off", "log", "--branches", "--not", "HEAD", "--tags",
            "--remotes", "--no-renames", "--name-only", "-z", "--format=")


def _parse_log(raw: bytes, rules: Rules) -> History:
    """Один проход вперёд: коммиты разделены \\x01, поля внутри — \\0, пары «мета, путь»."""
    history = History()
    for chunk in raw.split(b"\x01")[1:]:
        history.commits += 1
        fields = chunk.split(b"\0")[1:]
        for meta, pkg_raw in zip(fields[0::2], fields[1::2]):
            parts = meta.lstrip(b"\n").split(b" ")
            try:
                blob = parts[3].decode("ascii") if len(parts) >= 5 else ZERO_BLOB
            except UnicodeDecodeError:
                continue    # чужой вывод: пара с нечитаемым blob не доказательство, годные пары остаются
            if blob == ZERO_BLOB:
                continue
            place = _place(pkg_raw.decode("utf-8", "surrogateescape"), rules)
            if place is None:
                continue
            kind, dest = place
            target = history.blobs if kind == "in" else history.outside
            target.setdefault(dest, set()).add(blob)
    return history


def read_history(package_root, rules: Rules):
    if str(package_root) == "-":
        return NoHistory("пакет без истории")
    try:
        proc = _git(package_root, *LOG_ARGS)
        shallow = _git(package_root, "rev-parse", "--is-shallow-repository")
    except OSError as error:
        return NoHistory(error.strerror or str(error))
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return NoHistory(lines[0] if lines else f"git код {proc.returncode}")
    history = _parse_log(proc.stdout, rules)
    history.shallow = shallow.stdout.strip() == b"true"
    return history


def own_paths(package_root, rules: Rules):
    """Пути проекта, которых касались свои коммиты копии; NoHistory — git не ответил."""
    if str(package_root) == "-":
        return NoHistory("пакет без истории")
    try:
        proc = _git(package_root, *OWN_ARGS)
    except OSError as error:
        return NoHistory(error.strerror or str(error))
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return NoHistory(lines[0] if lines else f"git код {proc.returncode}")
    found = (project_path(raw.decode("utf-8", "surrogateescape"), rules)
             for raw in proc.stdout.split(b"\0") if raw)
    return {path for path in found if path}


def ours(history: History, rel: str, blob: str):
    """None — пути нет в истории; EMPTY — пустой файл (его содержимое не доказательство);
    иначе True/False — встречался ли именно этот blob по этому пути."""
    known = history.blobs.get(rel)
    if not known:
        return None
    if blob == EMPTY_BLOB:
        return EMPTY
    return blob in known


def unknown_files(project, known, absent) -> list:
    """Файлы корней укладки, которых нет ни в описи, ни в плане и чей ярус не запретный."""
    project = Path(project)
    found = []
    for root in layout_manifest.LAYOUT_ROOTS:
        for dirpath, dirnames, filenames in os.walk(project / root):
            # В истории пакета путей __pycache__ нет: судить их нечем.
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), project)
                if rel in known or layout_manifest.forbidden_tier(rel) != 0:
                    continue
                if layout_manifest.absent_dest(rel, absent):
                    continue
                found.append(rel)
    return sorted(found)


def seed_verdict(project, unknown, history: History) -> Seed:
    """Путь, которого история не знает, не читается: считается чужим по одному lstat."""
    project = str(project)
    real = os.path.realpath(project)
    seed = Seed({}, [], 0, 0, 0)
    for rel in unknown:
        if not layout_manifest.is_plain_file(project, real, rel):
            continue
        if rel not in history.blobs:
            seed.strangers += 1
            continue
        blob = layout_manifest.project_blob(project, real, rel)
        verdict = ours(history, rel, blob)
        if verdict == EMPTY:
            seed.empty += 1
        elif verdict:
            seed.would_remove[rel] = blob
        else:
            seed.foreign.append(rel)
    for rel, blobs in history.outside.items():
        if layout_manifest.project_blob(project, real, rel) in blobs:
            seed.outside += 1
    return seed


def _aware(value: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"время без пояса: {value}")
    return moment


def _rows(path: Path):
    """Список строк журнала; None — файла нет; Unreadable — не прочитан или строка битая
    (битая строка не равна пустой: пропуск согласия был бы тихим отказом)."""
    try:
        text = layout_journal.read_journal_text(str(path))
    except FileNotFoundError:
        return None
    except OSError as error:
        return Unreadable(f"{path.name}: {error.strerror}")
    except (ValueError, layout_journal.JournalBroken) as error:
        return Unreadable(f"{path.name}: {error}")
    rows = []
    # split по \n, не splitlines(): U+2028/U+2029/NEL внутри значения не конец строки журнала.
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("не объект")
            row["_ts"] = _aware(row["ts"])
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            return Unreadable(f"{path.name}:{number}: {error}")
        rows.append(row)
    return rows


def last_question(log_dir):
    rows = _rows(Path(log_dir) / ASKED_LOG)
    if not isinstance(rows, list):
        return rows
    if not rows:
        return None
    row = max(rows, key=lambda r: r["_ts"])
    paths = row.get("пути") or []
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return Unreadable(f"{ASKED_LOG}: «пути» не список строк")
    return Question(row["_ts"], paths)


def consent_after(question: Question, носитель=None):
    """Последнее «да, убрать» владельца в окне вопрос→ответ: позже вопроса и не позже
    7 суток после него. Окно от вопроса, а не от «сейчас»: раскладка идёт со следующим
    обновлением, и согласие не должно сгорать, если обновления реже раза в неделю.
    message_id обязателен — по нему видно, что слово пришло из канала, а не записано рукой.
    Папку слова даёт носитель (у машины — свой), а не каталог вопроса: вопрос лежит в LOG_DIR."""
    try:
        folder = (носитель if носитель is not None else owner_word.носитель()).папка
    except owner_word.НетНосителя as trouble:
        return Unreadable(str(trouble))
    rows = _rows(Path(folder) / CONSENT_LOG)
    if not isinstance(rows, list):
        return rows
    last = question.ts + dt.timedelta(days=CONSENT_MAX_AGE_DAYS)
    found = None
    for row in rows:
        text = str(row.get("text", ""))
        if not row.get("message_id"):
            continue
        if not (_WORD_YES.search(text) and _WORD_REMOVE.search(text)):
            continue
        if not question.ts < row["_ts"] <= last:
            continue
        found = {k: v for k, v in row.items() if k != "_ts"}
    return found


def _files_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "файл"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "файла"
    return "файлов"


def _question(asked) -> str:
    return (f"На сервере {len(asked)} {_files_word(len(asked))} от прежних версий харнеса: "
            f"{', '.join(asked)}. Убрать в архив? Ответь ровно «да, убрать».")


def question_text(paths, ceiling):
    """(текст, названные пути): текст растёт по одному пути, пока влезает в потолок канала;
    первый путь называется всегда. Журнал вопроса хранит ровно названные пути."""
    asked = list(paths[:1])
    for path in paths[1:]:
        if len(_question(asked + [path])) > ceiling:
            break
        asked.append(path)
    return _question(asked), asked


def record_question(log_dir, ts, asked, text) -> None:
    line = json.dumps({"ts": ts.isoformat(), "пути": list(asked), "вопрос": text},
                      ensure_ascii=False)
    # Эти три знака читатель с splitlines() считает концом строки; JSON-escape возвращает их дословно.
    for sep in ("\u2028", "\u2029", "\x85"):
        line = line.replace(sep, "\\u%04x" % ord(sep))
    with open(Path(log_dir) / ASKED_LOG, "a", encoding="utf-8") as out:
        out.write(line + "\n")


@dataclass
class SeedView:
    ts: dt.datetime
    paths: list
    line: str


def record_seed_view(log_dir, ts, paths, line) -> None:
    """Итог последнего прогона для `update.py --затравка`: что дверь назвала, пока шла раскладка."""
    target = Path(log_dir) / SEED_VIEW_LOG
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps({"ts": ts.isoformat(), "пути": list(paths), "строка": line},
                               ensure_ascii=False), encoding="utf-8")
    os.replace(temp, target)


def last_seed_view(log_dir):
    """SeedView | None — прогона ещё не было | Unreadable."""
    path = Path(log_dir) / SEED_VIEW_LOG
    try:
        row = json.loads(layout_journal.read_journal_text(str(path)))
        return SeedView(_aware(row["ts"]), list(row["пути"]), str(row["строка"]))
    except FileNotFoundError:
        return None
    except OSError as error:
        return Unreadable(f"{path.name}: {error.strerror}")
    except (ValueError, KeyError, TypeError, RecursionError, layout_journal.JournalBroken) as error:
        return Unreadable(f"{path.name}: {error}")
