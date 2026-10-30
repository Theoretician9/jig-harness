"""Журнал раскладки (journal.tsv архива прогона): разбор и суд отката.

Журнал пишет дверь укладки, здесь он только читается. Модуль на диск сам не ходит,
кроме поиска оборванных журналов в каталоге копий: решения отката судят диск и копии
архива прямого прогона, поданные вызывающим функциями.

Журнал — данные в каталоге копий, его можно подложить и его писала другая версия двери:
незнакомое действие и путь за проектом — отказ (откатывать вслепую нельзя), незнакомая
строка на «#» — не беда (новая версия пишет, старая откатывает).
"""
import os
import re

ACTIONS = ("перекрыт", "положен-новый", "снят", "возвращён")
NEW_VERSION_ACTIONS = ("перекрыт", "положен-новый", "снят")
ARCHIVE_NAME = "(%s)-[0-9]{8}-[0-9]{6}-[0-9]+(-откат)?"
MANIFEST_COPY_PREFIX = "# опись до прогона: files/.layout-manifest "
MANIFEST_MISSING_PREFIX = "# опись не снята: "
KEPT_PREFIX = "# оставлен\t"
SEED_PREFIX = "# по истории пакета: "
NOT_A_FILE = "не файл"

_BLOB = re.compile(r"[0-9a-f]{40}\Z")
_HEADER = re.compile(r"# прогон ([0-9]+) (обновление|раскладка|откат) план=([0-9]+) "
                     r"перекрыть=([0-9]+)(?: по=(.+))?\Z")
# Which of the two blob fields an action must carry; the other may be «-» or a blob.
_REQUIRED = {"перекрыт": (True, True), "положен-новый": (False, True),
             "снят": (True, False), "возвращён": (False, True)}


class JournalBroken(Exception):
    """Журнал, который нельзя читать: пустой Journal из него не получается никогда."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Action:
    __slots__ = ("kind", "was", "now", "path", "done")

    def __init__(self, kind: str, was: str, now: str, path: str) -> None:
        self.kind, self.was, self.now, self.path = kind, was, now, path
        self.done = False

    def __repr__(self) -> str:
        return f"Action({self.kind!r}, {self.was!r}, {self.now!r}, {self.path!r}, {self.done})"


class Journal:
    def __init__(self) -> None:
        self.mode: str | None = None
        self.forward_of: str | None = None
        self.actions: list = []
        self.has_total = False
        self.resolved_by: list = []
        self.kept: list = []
        self.seed_paths: set = set()
        self.manifest_copy: str | None = None
        self.manifest_missing: str | None = None

    @property
    def undone(self) -> int:
        return sum(1 for action in self.actions if not action.done)


def _bad_path(path: str) -> bool:
    return not path or path.startswith("/") or ".." in path.split("/")


def _action(fields: list, line: str) -> Action:
    kind, was, now, path = fields
    for value, required in zip((was, now), _REQUIRED[kind]):
        if not (_BLOB.match(value) or (value == "-" and not required)):
            raise JournalBroken(f"blob не из 40 знаков hex: {line!r}")
    if _bad_path(path):
        raise JournalBroken(f"путь за проектом: {line!r}")
    return Action(kind, was, now, path)


def _comment(journal: Journal, line: str) -> None:
    if line.startswith(MANIFEST_COPY_PREFIX):
        journal.manifest_copy = line[len(MANIFEST_COPY_PREFIX):]
    elif line.startswith(MANIFEST_MISSING_PREFIX):
        journal.manifest_missing = line[len(MANIFEST_MISSING_PREFIX):]
    elif line.startswith(SEED_PREFIX):
        journal.seed_paths.add(line[len(SEED_PREFIX):])
    elif line.startswith(KEPT_PREFIX):
        fields = line.split("\t")
        if len(fields) != 3 or _bad_path(fields[2]):
            raise JournalBroken(f"строка журнала не понята: {line!r}")
        journal.kept.append((fields[1], fields[2]))


def _close_action(journal: Journal, path: str) -> None:
    """«Готово» закрывает последнее открытое действие ЭТОГО пути; чужой путь ничего не закрывает."""
    for action in reversed(journal.actions):
        if action.path == path and not action.done:
            action.done = True
            return


def _header(journal: Journal, line: str) -> None:
    match = _HEADER.match(line)
    if not match:
        raise JournalBroken(f"шапка журнала не понята: {line!r}")
    journal.mode = match.group(2)
    journal.forward_of = match.group(5)


def _body_line(journal: Journal, line: str) -> None:
    if line.startswith("#"):
        _comment(journal, line)
        return
    fields = line.split("\t")
    if len(fields) != 4:
        raise JournalBroken(f"строка журнала не понята: {line!r}")
    word = fields[0]
    if word in ACTIONS:
        journal.actions.append(_action(fields, line))
    elif word == "готово":
        _close_action(journal, fields[3])
    elif word == "итог":
        journal.has_total = True
    elif word == "разобран":
        journal.resolved_by.append(fields[3])
    else:
        raise JournalBroken(f"строка журнала не понята: {line!r}")


def parse_journal(text: str) -> Journal:
    journal = Journal()
    lines = text.split("\n")
    lines.pop()     # последняя строка без перевода строки — обрыв посреди записи, не действие
    for index, line in enumerate(lines):
        if index == 0:
            _header(journal, line)
        else:
            _body_line(journal, line)
    return journal


def archive_name_pattern(project_name: str) -> re.Pattern:
    """Точное имя проекта: маска «<имя>-*» поймала бы архивы проекта «<имя>-x» второй установки."""
    return re.compile("^" + ARCHIVE_NAME % re.escape(project_name) + r"\Z")


# Журнал — сотни строк; предел нужен нерегулярному файлу (симлинк на /dev/zero читается вечно).
JOURNAL_LIMIT = 64 * 1024 * 1024


def read_journal_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        text = handle.read(JOURNAL_LIMIT + 1)
    if len(text) > JOURNAL_LIMIT:
        raise JournalBroken("журнал слишком велик")
    return text


def read_journal(archive) -> tuple:
    """→ (журнал | None, причина | None). Нет файла — (None, None): раскладка архив не открывала.
    Нечитаемый или чужого формата — причина словами: «нет» и «не прочитан» разные ответы."""
    try:
        return parse_journal(read_journal_text(os.path.join(str(archive), "journal.tsv"))), None
    except FileNotFoundError:
        return None, None
    except OSError as trouble:
        return None, trouble.strerror or str(trouble)
    except (ValueError, JournalBroken) as trouble:
        return None, str(trouble)


def _journals(root: str, project_name: str):
    """(архив, журнал, причина) своих архивов проекта; журнал None — не прочитан (причина) или не открыт."""
    pattern = archive_name_pattern(project_name)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for name in names:
        archive = os.path.join(root, name)
        if pattern.match(name) and os.path.isdir(archive):
            journal, reason = read_journal(archive)
            if journal is not None or reason:
                yield archive, journal, reason


def _real(path) -> str:
    return os.path.realpath(str(path))


def find_orphans(raskladka_root: str, project_name: str, exclude: set) -> list:
    """[(архив, строк без «готово» | None, причина)] журналов без «итог» и без «разобран».
    Нечитаемый журнал не молчит: строк нет (None), причина словами."""
    skip = {_real(path) for path in exclude}
    found = []
    for archive, journal, reason in _journals(raskladka_root, project_name):
        if _real(archive) in skip:
            continue
        if journal is None:
            found.append((archive, None, reason))
        elif not journal.has_total and not journal.resolved_by:
            found.append((archive, journal.undone, ""))
    return found


def resolved_by_archive(raskladka_root: str, project_name: str, archives: set) -> list:
    """[(архив, строк без «готово»)] оборванных журналов, чья строка «разобран» называет один из archives.
    Журнал с «итог» оборванным не был: «разобран» в нём только метка прогона."""
    wanted = {_real(path) for path in archives}
    return [(archive, journal.undone) for archive, journal, _ in _journals(raskladka_root, project_name)
            if journal is not None and not journal.has_total
            and any(_real(name) in wanted for name in journal.resolved_by if name != "-")]


class Decisions:
    """Что откат сделает с каждым путём прямого журнала."""

    def __init__(self) -> None:
        self.remove: dict = {}        # путь → blob новой версии на диске
        self.restore: dict = {}       # путь → blob, который лежит в копии архива прямого
        self.keep: dict = {}          # путь → причина, по которой откат путь не трогает
        self.relaid_new: list = []    # «положен-новый» путь плана A: его положит укладка A
        self.undone = 0               # действий прямого журнала без «готово»


def _decide_new(action: Action, plan_a: dict, disk, found: Decisions) -> None:
    path = action.path
    if path in plan_a:
        found.relaid_new.append(path)
    elif disk == action.now:
        found.remove[path] = action.now
    elif disk is None:
        # Без «готово» и без файла замена не дошла до диска: убирать нечего, оставлять нечего.
        if action.done:
            found.keep[path] = "уже не было"
    elif disk == NOT_A_FILE:
        found.keep[path] = NOT_A_FILE
    else:
        found.keep[path] = "правлен после обновления"


def _decide_overwritten(action: Action, plan_a: dict, disk, copy, found: Decisions,
                        mode_differs: bool) -> None:
    path, own = action.path, action.was
    # Тот же blob не значит тот же файл: укладка A кладёт режим пакета, копия хранит режим владельца.
    if plan_a.get(path) == own and not mode_differs:
        return
    if not action.done and disk == own:
        return          # Stop до replace: файл так и остался своим
    if disk is None:
        found.keep[path] = "уже не было"
    elif disk == NOT_A_FILE:
        found.keep[path] = NOT_A_FILE
    elif disk not in (plan_a.get(path), action.now):
        found.keep[path] = "правлен после обновления"
    # Возврат из копии, не из «was» журнала: blob в журнале — запись, байты лежат в копии.
    elif copy == own:
        found.restore[path] = own
    else:
        found.keep[path] = "копии нет в архиве"


def _decide_removed(action: Action, plan_a: dict, disk, copy, found: Decisions) -> None:
    path = action.path
    if path in plan_a or disk == action.was:
        return          # укладка A кладёт файл плана; диск = X — снятие не дошло (EXDEV)
    if disk is None:
        # Копия может быть правленой (Stop после rename): владельцу возвращается то, что
        # лежит в архиве, пока пути в проекте нет.
        if copy is None:
            found.keep[path] = "копии нет в архиве"
        else:
            found.restore[path] = copy
    else:
        found.keep[path] = "занято"


def rollback_decisions(journal: Journal, plan_a: dict, disk_blob, forward_copy_blob,
                       mode_differs=lambda path: False) -> Decisions:
    """Таблица отката — единственное место. Порядок суда: действие → путь в плане A → диск
    после укладки A → копия в архиве прямого. «Готово» не судит (его нет при обрыве между
    записью и строкой): судит диск."""
    found = Decisions()
    found.undone = journal.undone
    for action in journal.actions:
        if action.kind == "положен-новый":
            _decide_new(action, plan_a, disk_blob(action.path), found)
        elif action.kind == "перекрыт":
            _decide_overwritten(action, plan_a, disk_blob(action.path),
                                forward_copy_blob(action.path), found,
                                mode_differs(action.path))
        elif action.kind == "снят":
            _decide_removed(action, plan_a, disk_blob(action.path),
                            forward_copy_blob(action.path), found)
    return found


def rollback_outcome(journal: Journal) -> dict:
    """Итог по журналу отката: считаются только действия с «готово»."""
    done = [action.kind for action in journal.actions if action.done]
    return {"по журналу": journal.mode == "откат",
            "снято": done.count("снят"),
            "возвращено": done.count("возвращён"),
            "оставлено": list(journal.kept),
            "строк без готово": journal.undone}


def count_new_version_files(journal: Journal) -> int:
    return sum(1 for action in journal.actions if action.kind in NEW_VERSION_ACTIONS)
