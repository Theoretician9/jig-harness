#!/usr/bin/env -S python3 -I
"""Дверь укладки: единственное место, где файл пакета ложится в проект.

Нет копии — нет перезаписи. Дверь идёт двумя ходами:
  ход 1 — предпроверка: сличает план с проектом и готовит архив; в проект не пишет
          ничего. Любая беда — «РАСКЛАДКА НЕ НАЧАТА», все беды списком, код 3.
  ход 2 — запись в порядке плана: строка журнала → копия → временный файл →
          сверка blob → os.replace → «готово». Сбой середины — код 1.

Лишнее по прежней описи (было положено дверью, пакет его больше не несёт и на диске оно
нетронуто) дверь не удаляет, а переносит в files/ архива прогона: строка «снят» в журнал
ДО переноса, «готово» после. Судит о лишнем модуль описи в ходе 1: так недоступный архив
отказывает до первой записи в проект.

После удачного хода 2 дверь обновляет опись <PROJECT_DIR>/.layout-manifest: что
она положила, что снято и что осталось.

Затравка: файл корней укладки, которого нет ни в плане, ни в описи, дверь снимает в архив, только
если его путь И содержимое встречались в истории пакета (--package-root) и владелец после вопроса
(update.py) ответил «да, убрать». Без ответа файл лишь называется.

Откат (--rollback-of <архив прямого прогона>): та же дверь кладёт план прежней версии, затем по
журналу прямого прогона снимает то, что новая версия положила, и возвращает своё из его копий.
Что судит диск, а не строка «готово»: журнал обрывается между записью и «готово».

Запуск: layout-door.py --plan <файл> --project <PROJECT_DIR> [--package-commit <sha|->]
                       [--package-root <каталог|->]
                       [--lift-threshold <причина> | --rollback-of <архив>]
План: записи «<группа>\\t<источник абс.>\\t<назначение от PROJECT_DIR>» через \\0.
Источник «-» значит «группа без источника в этом пакете»: не беда, а пометка для описи.
"""
import datetime
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402
import layout_history  # noqa: E402
import layout_journal  # noqa: E402
import layout_manifest  # noqa: E402

EXIT_REFUSED = 3
# Без обязательных аргументов дверь раньше шла дальше и падала не по делу: справка и код 2.
EXIT_USAGE = 2
HELP_ARGS = {"--help", "-h"}
REQUIRED_ARGS = ("--plan", "--project")
EXIT_FAILED = 1
MARKER = "РАСКЛАДКА НЕ НАЧАТА: "
# skills пересобирает генератор через rmtree: копия ничего бы не защитила.
REBUILT_GROUP = "skills"
LIMIT_KEY = "LAYOUT_REMOVE_MAX_PERCENT"
LIFT_ARG = "--lift-threshold"
ROLLBACK_ARG = "--rollback-of"
PACKAGE_ROOT_ARG = "--package-root"
# Пометка перед «снят»: слово действия новое не вводится (откат Ф-г прочёл бы его как беду),
# а незнакомая строка «#» старые разборщики пропускают.
SEED_MARK = "# по истории пакета: "
HISTORY_RULES = ("harness", "config", "layout-history-paths.yaml")
# Слова суда отката и слова описи (KEPT_REASONS) — разные: перевод в одном месте.
KEPT_WORDS = {"правлен здесь": "правлен после обновления"}
OCCUPIED = "занято"
SELFTEST_DOORS = ("HARNESS_LAYOUT_KILL_AFTER_WRITE", "HARNESS_LAYOUT_PLAN_OUT")


class Entry:
    __slots__ = ("group", "src", "rel", "dst", "state", "blob_new", "blob_old", "mode")

    def __init__(self, group: str, src: str, rel: str, dst: str) -> None:
        self.group, self.src, self.rel, self.dst = group, src, rel, dst
        self.state = "new"
        self.blob_new = ""
        self.blob_old = ""
        self.mode = 0

    @property
    def needs_copy(self) -> bool:
        return self.state == "differs" and self.group != REBUILT_GROUP

    @property
    def writes(self) -> bool:
        return self.state != "same"


class Trouble:
    """Беда хода 1: короткая причина для первой строки и подробные строки."""

    def __init__(self, short: str, details: list | None = None) -> None:
        self.short = short
        self.details = details or [short]


def blob(path: str) -> str:
    """То же, что `git hash-object <path>`, без git."""
    return layout_manifest.git_blob(path)


def write_journal_line(fd: int, text: str) -> None:
    """Короткая запись «удаётся» без явной сверки числа байт (замер 19.10)."""
    data = (text + "\n").encode("utf-8")
    try:
        written = os.write(fd, data)
    except OSError as error:
        raise OSError(f"журнал не записан: {os.strerror(error.errno)}") from None
    if written != len(data):
        raise OSError(f"журнал не записан: записано {written} из {len(data)} байт")
    os.fsync(fd)


def copy_to_archive(src: str, dst_in_archive: str) -> str:
    """Копия с режимом и временем; blob считается по ФАЙЛУ копии, не по источнику."""
    os.makedirs(os.path.dirname(dst_in_archive), exist_ok=True)
    shutil.copy2(src, dst_in_archive)
    with open(dst_in_archive, "rb") as copy:
        os.fsync(copy.fileno())
    return blob(dst_in_archive)


def is_inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def files_noun(count: int) -> str:
    tail = count % 100
    if tail % 10 == 1 and tail != 11:
        return "файл"
    if 2 <= tail % 10 <= 4 and not 12 <= tail <= 14:
        return "файла"
    return "файлов"


def plural_files(count: int) -> str:
    noun = files_noun(count)
    return f"{count} {noun} {'отличается' if noun == 'файл' else 'отличаются'} от пакета"


def parse_plan(raw: bytes) -> list:
    return [record for record in raw.decode("utf-8").split("\0") if record]


def nearest_existing(path: str) -> str:
    while not os.path.lexists(path):
        path = os.path.dirname(path)
    return path


def classify(record: str, project: str, project_real: str, troubles: list, absent: list,
             unplaced: list):
    """Одна запись плана → Entry или None (беда названа, запись выпала или ушла в absent)."""
    fields = record.split("\t")
    if len(fields) != 3 or "\n" in record:
        troubles.append(Trouble(f"запись плана разобрать нельзя: {record!r}"))
        return None
    group, src, rel = fields
    parts = rel.split("/")
    if rel.startswith("/") or ".." in parts or not rel:
        troubles.append(Trouble(f"за проектом: {rel}"))
        return None
    if src == "-":
        absent.append((group, rel))
        return None
    if os.path.islink(src):
        print(f"источник — ссылка, не положен: {src}")
        unplaced.append(rel)
        return None
    if not os.path.isfile(src):
        troubles.append(Trouble(f"источник не обычный файл: {src}"))
        return None
    dst = os.path.join(project, rel)
    parent = os.path.realpath(nearest_existing(os.path.dirname(dst)))
    if not is_inside(parent, project_real):
        troubles.append(Trouble(f"за проектом: {rel}"))
        return None
    if not os.path.isdir(parent):
        troubles.append(Trouble(f"родитель назначения не каталог: {rel}"))
        return None
    entry = Entry(group, src, rel, dst)
    try:
        readable = read_state(entry, troubles)
    except OSError as error:
        # Код 1 значит «сбой середины»; нечитаемый файл — отказ до записи, проект цел.
        troubles.append(Trouble(f"не читается: {error.filename}: {os.strerror(error.errno)}"))
        return None
    return entry if readable else None


def read_state(entry: Entry, troubles: list) -> bool:
    """Сличает источник с назначением; нечитаемое поднимает OSError. False — беда названа."""
    entry.blob_new = blob(entry.src)
    entry.mode = stat.S_IMODE(os.stat(entry.src).st_mode)
    if not os.path.lexists(entry.dst):
        return True
    if not stat.S_ISREG(os.lstat(entry.dst).st_mode):
        troubles.append(Trouble(f"назначение не обычный файл: {entry.rel}"))
        return False
    entry.blob_old = blob(entry.dst)
    entry.state = "same" if entry.blob_old == entry.blob_new else "differs"
    return True


def check_remnants(destinations: list, troubles: list) -> list:
    """Остаток оборванной раскладки: имя зарезервировано за дверью, данных в нём нет."""
    notes = []
    for dst in destinations:
        leftover = os.path.join(os.path.dirname(dst), f".{os.path.basename(dst)}.layout-tmp")
        if not os.path.lexists(leftover):
            continue
        if not stat.S_ISREG(os.lstat(leftover).st_mode):
            troubles.append(Trouble(f"остаток раскладки не обычный файл: {leftover}"))
            continue
        notes.append(f"остаток оборванной раскладки: {leftover}")
    return notes


def check_duplicates(entries: list, troubles: list) -> None:
    """Второе перекрытие одного пути затёрло бы копию подлинника копией промежуточного."""
    first_group: dict = {}
    for entry in entries:
        if entry.rel in first_group:
            troubles.append(Trouble(f"назначение дважды в плане: {entry.rel} "
                                    f"({first_group[entry.rel]}, {entry.group})"))
        first_group.setdefault(entry.rel, entry.group)


class Archive:
    def __init__(self, path: str, fd: int) -> None:
        self.path = path
        self.fd = fd
        self.actions = 0

    def log(self, action: str, was: str, now: str, rel: str) -> None:
        write_journal_line(self.fd, f"{action}\t{was}\t{now}\t{rel}")
        if action in ("перекрыт", "положен-новый", "снят", "возвращён"):
            self.actions += 1


def archive_path(backups: Path, explicit: str, project_name: str, troubles: list):
    """Путь архива: заданный обязан лежать внутри <копии>/раскладка (граница §0)."""
    root = os.path.realpath(backups / "раскладка")
    if explicit:
        if not is_inside(os.path.realpath(explicit), root) or os.path.realpath(explicit) == root:
            troubles.append(Trouble(f"архив вне каталога копий: {explicit}"))
            return None
        return explicit
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(Path(root) / f"{project_name}-{stamp}-{os.getpid()}")


def selftest_target_ok(backups: Path) -> bool:
    """Самотест не пишет в живое: каталог копий — внутри каталога паспорта стенда."""
    conf = os.environ.get("HARNESS_INSTALL_CONF")
    if not conf:
        return False
    stand = os.path.realpath(os.path.dirname(conf))
    if stand == os.path.realpath(config.корень_машины()):
        return False
    return is_inside(os.path.realpath(backups), stand)


def no_room_trouble(overwrites: list, path: str, error_number: int) -> Trouble:
    short = (plural_files(len(overwrites)) + ", а копию снять некуда (ключ BACKUP_DIR)"
             if overwrites else "журнал обновления вести негде (ключ BACKUP_DIR)")
    reason = f"копию снять некуда: {path}: {os.strerror(error_number)}"
    lines = [f"{e.rel} отличается от пакета, {reason}" for e in overwrites] or [reason]
    return Trouble(short, lines)


def removal_room_trouble(removals: list, path: str, error_number: int) -> Trouble:
    short = (f"{len(removals)} {files_noun(len(removals))} прежней версии к снятию, "
             "а архив недоступен (ключ BACKUP_DIR)")
    reason = os.strerror(error_number)
    return Trouble(short, [f"{rel} к снятию, архив недоступен: {path}: {reason}"
                           for rel in removals])


def archive_troubles(overwrites: list, removals: list, path: str, error_number: int) -> list:
    """Беда архива по тому, ради чего он нужен: перекрытие, снятие, оба. Только журнал —
    «вести негде»."""
    found = []
    if overwrites or not removals:
        found.append(no_room_trouble(overwrites, path, error_number))
    if removals:
        found.append(removal_room_trouble(removals, path, error_number))
    return found


def plan_archive(overwrites: list, removals: list, selftest: bool, troubles: list):
    """Все беды архива до первой записи: путь или None. Ничего не создаёт.
    removals — пути (от проекта), которые уйдут в files/ архива."""
    explicit = os.environ.get("HARNESS_LAYOUT_ARCHIVE", "")
    try:
        backups = config.backup_dir()
    except LookupError as refusal:
        troubles.append(Trouble(str(refusal)))
        return None
    if selftest and not selftest_target_ok(backups):
        troubles.append(Trouble(f"самотест целится в живой каталог копий: {backups}"))
        return None
    name = ""
    if not explicit:
        install_conf = config.путь_install_conf()
        name = config.конфиг(install_conf).get("PROJECT_NAME", "")
        if not name:
            troubles.append(Trouble(f"PROJECT_NAME пуст в {install_conf}"))
            return None
    path = archive_path(backups, explicit, name, troubles)
    if path is None:
        return None
    wanted = [e.rel for e in overwrites] + list(removals)
    troubles.extend(Trouble(f"копия уже есть в архиве: {rel}") for rel in wanted
                    if os.path.lexists(os.path.join(path, "files", rel)))
    anchor = nearest_existing(path)
    if not os.path.isdir(anchor):
        troubles.extend(archive_troubles(overwrites, removals, path, errno.ENOTDIR))
    elif not os.access(anchor, os.W_OK | os.X_OK):
        troubles.extend(archive_troubles(overwrites, removals, path, errno.EACCES))
    return path


def open_archive(path: str, entries: list, overwrites: list, removals: list, lift: str,
                 troubles: list, rollback_of: str = ""):
    """Каталог архива и журнал с шапкой: зовётся, когда ход 2 точно начнётся."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as error:
        troubles.extend(archive_troubles(overwrites, removals, path, error.errno))
        return None
    try:
        fd = os.open(os.path.join(path, "journal.tsv"),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        mode = "обновление" if os.environ.get("HARNESS_LAYOUT_ARCHIVE") else "раскладка"
        if rollback_of:
            mode = "откат"
        write_journal_line(fd, f"# прогон {os.getpid()} {mode} план={len(entries)} "
                               f"перекрыть={len(overwrites)}"
                               + (f" по={rollback_of}" if rollback_of else ""))
        if lift:
            write_journal_line(fd, f"# снять сверх порога: {lift}")
    except OSError as error:
        troubles.append(Trouble(str(error) if str(error).startswith("журнал не записан")
                                else f"журнал не открыт: {path}: {os.strerror(error.errno)}"))
        return None
    return Archive(path, fd)


class PriorManifest:
    """Прежняя опись, как её нашёл ход 1.

    raw None — описи нет. parsed None при raw — нечитаема (reason); copyable —
    содержимое прочлось и его можно снять копией в архив; sheltered — путь копии,
    unavailable — почему копию снять не вышло."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.raw: bytes | None = None
        self.parsed = None
        self.reason = ""
        self.copyable = False
        self.sheltered = ""
        self.unavailable = ""

    @property
    def broken(self) -> bool:
        return self.raw is not None and self.parsed is None


def read_prior(project: str) -> PriorManifest:
    prior = PriorManifest(os.path.join(project, layout_manifest.MANIFEST_NAME))
    try:
        mode = os.lstat(prior.path).st_mode
    except FileNotFoundError:
        return prior
    except OSError as error:
        prior.raw, prior.reason = b"", os.strerror(error.errno)
        return prior
    if not stat.S_ISREG(mode):
        prior.raw, prior.reason = b"", "не обычный файл"
        return prior
    try:
        prior.raw = Path(prior.path).read_bytes()
    except OSError as error:
        prior.raw, prior.reason = b"", os.strerror(error.errno)
        return prior
    parsed = layout_manifest.parse_bytes(prior.raw)
    prior.copyable = True
    if isinstance(parsed, layout_manifest.ManifestBroken):
        prior.reason = parsed.reason
    else:
        prior.parsed = parsed
    return prior


def fail_shelter(prior: PriorManifest, archive: Archive, reason: str) -> Archive:
    """Копия описи не снята: причина идёт и в журнал, чтобы откат сказал «не снята», а не «описи не было»."""
    prior.unavailable = reason
    try:
        write_journal_line(archive.fd, layout_journal.MANIFEST_MISSING_PREFIX
                           + reason.replace("\t", " ").replace("\n", " "))
    except OSError:
        pass
    return archive


def shelter_prior(prior: PriorManifest, archive: Archive | None, entries: list,
                  selftest: bool) -> Archive | None:
    """Копия прежней описи в архив прогона до замены: нечитаемой — чтобы не пропала, читаемой
    при обновлении — чтобы откат вернул опись «до». Архива ради неё может не быть: тогда он
    открывается здесь, по тем же правилам, что и ради файлов."""
    if archive is None:
        local: list = []
        path = plan_archive([], [], selftest, local)
        if path is None or local:
            prior.unavailable = local[0].short if local else "архив не определён"
            return None
        if os.path.lexists(os.path.join(path, "files", layout_manifest.MANIFEST_NAME)):
            prior.unavailable = "копия уже есть в архиве"
            return None
        archive = open_archive(path, entries, [], [], "", local)
        if archive is None:
            prior.unavailable = local[0].short
            return None
    copy = os.path.join(archive.path, "files", layout_manifest.MANIFEST_NAME)
    if os.path.lexists(copy):
        return fail_shelter(prior, archive, "копия уже есть в архиве")
    try:
        copy_blob = copy_to_archive(prior.path, copy)
    except OSError as error:
        return fail_shelter(prior, archive, f"{error.filename or copy}: {os.strerror(error.errno)}")
    if copy_blob != blob_of_bytes(prior.raw):
        os.unlink(copy)
        return fail_shelter(prior, archive, "копия не совпала с описью")
    # Строки о копии пишутся после сверки: журнал, назвавший копию, которой нет, увёл бы откат
    # к «описи не было», а не «не снята».
    try:
        if prior.broken:
            write_journal_line(archive.fd,
                               f"# опись нечитаема: files/{layout_manifest.MANIFEST_NAME}")
        write_journal_line(archive.fd, layout_journal.MANIFEST_COPY_PREFIX
                           + blob_of_bytes(prior.raw))
    except OSError as error:
        prior.unavailable = f"журнал: {os.strerror(error.errno)}"
        return archive
    prior.sheltered = copy
    return archive


def blob_of_bytes(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Removals:
    """Что снять по прежней описи и что вышло: rows — (путь, blob описи) в порядке пути."""

    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.done: set = set()
        self.skipped: dict = {}
        self.seeded: set = set()

    def add_seeded(self, approved: dict) -> None:
        """Согласованное по истории: blob — диска, порог их не судит (владелец назвал их поимённо)."""
        self.rows = sorted(self.rows + list(approved.items()))
        self.seeded |= set(approved)

    @property
    def paths(self) -> list:
        return [rel for rel, _blob in self.rows]


class Prepared:
    """Итог хода 1: всё, что нужно ходу 2 и описи."""

    def __init__(self) -> None:
        self.entries: list = []
        self.archive: Archive | None = None
        self.notes: list = []
        self.troubles: list = []
        self.absent: list = []
        self.unplaced: list = []
        self.seed_lines: list = []
        self.prior: PriorManifest | None = None
        self.verdict = None
        self.removals = Removals([])
        self.rollback: Rollback | None = None


class Rollback:
    """Откат, как его решил ход 1: журнал прямого прогона и что с каждым путём."""

    def __init__(self, given: str, real: str, journal, decisions, expected: dict,
                 journal_exists: bool) -> None:
        self.given = given                  # как назван в доводе: так он и ложится в шапку журнала
        self.real = real
        self.journal = journal
        self.decisions = decisions
        self.expected = expected            # путь → blob на диске, поверх которого пишет возврат
        self.journal_exists = journal_exists
        self.kept = dict(decisions.keep)    # путь → причина; пополняется ходом 2
        self.restored: set = set()

    @property
    def files(self) -> str:
        return os.path.join(self.real, "files")


project_blob = layout_manifest.project_blob


def archive_copy_blob(files_root: str, rel: str) -> str | None:
    full = os.path.join(files_root, rel)
    try:
        if not stat.S_ISREG(os.lstat(full).st_mode):
            return None
        if not is_inside(os.path.realpath(full), os.path.realpath(files_root)):
            return None
        return blob(full)
    except OSError:
        return None


def read_forward_journal(archive: str, troubles: list):
    """(журнал, есть ли он на диске). Нет файла — прямой прогон ничего не писал: пустой журнал."""
    path = os.path.join(archive, "journal.tsv")
    if not os.path.lexists(path):
        return layout_journal.Journal(), False
    try:
        journal = layout_journal.parse_journal(layout_journal.read_journal_text(path))
    except layout_journal.JournalBroken as broken:
        troubles.append(Trouble(f"журнал прямого прогона не читается: {broken.reason}"))
        return None, True
    except (OSError, UnicodeDecodeError) as error:
        reason = os.strerror(error.errno) if isinstance(error, OSError) and error.errno else str(error)
        troubles.append(Trouble(f"журнал прямого прогона не читается: {reason}"))
        return None, True
    if journal.mode == "откат":
        troubles.append(Trouble("журнал прямого прогона не читается: это журнал отката"))
        return None, True
    return journal, True


def judge_rollback(given: str, entries: list, project: str, troubles: list, notes: list):
    """Ход 1 отката: что делать с каждым путём журнала. В проект не пишет."""
    try:
        backups = config.backup_dir()
    except LookupError:
        return None         # ту же беду называет plan_archive, дважды её не пишем
    install_conf = config.путь_install_conf()
    name = config.конфиг(install_conf).get("PROJECT_NAME", "")
    if not name:
        troubles.append(Trouble(f"PROJECT_NAME пуст в {install_conf}"))
        return None
    real, root = os.path.realpath(given), os.path.realpath(backups / "раскладка")
    if (not is_inside(real, root) or real == root
            or not layout_journal.archive_name_pattern(name).match(os.path.basename(real))):
        troubles.append(Trouble(f"архив прямого прогона вне каталога копий: {given}"))
        return None
    journal, exists = read_forward_journal(real, troubles)
    if journal is None:
        return None
    plan_a = {e.rel: e.blob_new for e in entries}
    project_real = os.path.realpath(project)
    files_root = os.path.join(real, "files")

    def disk(rel: str):
        return plan_a[rel] if rel in plan_a else project_blob(project, project_real, rel)

    plan_modes = {e.rel: e.mode for e in entries}

    def mode_differs(rel: str) -> bool:
        try:
            copy_mode = stat.S_IMODE(os.lstat(os.path.join(files_root, rel)).st_mode)
        except OSError:
            return False
        return rel in plan_modes and copy_mode != plan_modes[rel]

    decisions = layout_journal.rollback_decisions(
        journal, plan_a, disk, lambda rel: archive_copy_blob(files_root, rel), mode_differs)
    outside_plan = sorted({a.path for a in journal.actions if a.path not in plan_a})
    notes.extend(check_remnants([os.path.join(project, rel) for rel in outside_plan], troubles))
    notes.extend(f"положен укладкой A, до обновления не было: {rel}"
                 for rel in sorted(decisions.relaid_new))
    return Rollback(given, real, journal, decisions,
                    {rel: disk(rel) for rel in decisions.restore}, exists)


class PackageHistory:
    """История пакета читается лениво и один раз: чистая установка git не зовёт."""

    def __init__(self, root: str) -> None:
        self.root = root
        self._found = None
        self._own = None
        self._rules = None

    def get(self):
        if self._found is None:
            self._found = self._read()
        return self._found

    def _read(self):
        if self.root == "-":
            return layout_history.NoHistory("пакет без истории")
        table = Path(self.root, *HISTORY_RULES)
        try:
            rules = layout_history.load_rules(table)
        except (OSError, yaml.YAMLError, AttributeError) as error:
            return layout_history.NoHistory(f"таблица перевода не прочитана: {table}: {error}")
        self._rules = rules
        return layout_history.read_history(self.root, rules)

    def own(self):
        """Своё копии: множество путей проекта или NoHistory; git зовётся один раз."""
        if self._own is None:
            found = self.get()
            self._own = found if isinstance(found, layout_history.NoHistory) else (
                layout_history.own_paths(self.root, self._rules))
        return self._own


def history_ours(history: PackageHistory, project: str):
    """ours для суда описи: blob диска, если правка — прежняя версия нашего файла."""
    project_real = os.path.realpath(project)

    def ours(rel: str):
        found = history.get()
        if isinstance(found, layout_history.NoHistory):
            return None
        disk = project_blob(project, project_real, rel)
        if disk in (None, layout_journal.NOT_A_FILE):
            return None
        return disk if layout_history.ours(found, rel, disk) is True else None

    return ours


def judge_prior(prior: PriorManifest, entries: list, absent: list, project: str, lift: str,
                ours=None, own=frozenset()):
    """Суд о лишнем по читаемой прежней описи; None — судить не по чему."""
    if prior.parsed is None:
        return None, Removals([])
    verdict = layout_manifest.judge_extra(prior.parsed, manifest_plan(entries), absent, project,
                                          removal_limit(), lift=bool(lift),
                                          lift_only=prior.parsed.deferred, ours=ours,
                                          own=own)
    removals = Removals([(rel, verdict.seeded.get(rel) or row_blob(prior.parsed, rel))
                         for rel in sorted(verdict.would_remove)])
    removals.seeded = set(verdict.seeded) & verdict.would_remove
    return verdict, removals


# Машинная строка для update.py: список путей без разбора человеческой строки по «, ».
SEED_LIST_PREFIX = "список затравки: "


def unreadable_consent(reason: str) -> list:
    return [f"согласие не прочитано ({reason}) — по истории не снимаю"]


def seed_consent(candidates: dict, selftest: bool):
    """(согласованные {путь: blob}, строки): «да, убрать» после вопроса, назвавшего путь."""
    try:
        log_dir = config.log_dir()
    except KeyError:
        return {}, unreadable_consent("LOG_DIR не задан")
    if selftest and not selftest_target_ok(Path(log_dir)):
        return {}, unreadable_consent("самотест целится в живой LOG_DIR")
    question = layout_history.last_question(log_dir)
    if question is None:
        return {}, []
    if isinstance(question, layout_history.Unreadable):
        return {}, unreadable_consent(question.reason)
    consent = layout_history.consent_after(question)
    if isinstance(consent, layout_history.Unreadable):
        return {}, unreadable_consent(consent.reason)
    if consent is None:
        return {}, [f"вопрос о затравке задан {question.ts:%Y-%m-%d %H:%M}, "
                    "«да, убрать» после него нет"]
    return {rel: blob for rel, blob in candidates.items() if rel in set(question.paths)}, []


def seed_summary(seed, shallow: bool) -> list:
    lines = ["история пакета неполная (shallow) — затравка называет только найденное"] if shallow else []
    lines += [f"не наш по содержимому: {rel}" for rel in sorted(seed.foreign)]
    if seed.strangers:
        lines.append(f"чужих вне описи {seed.strangers} — история пакета их не знает")
    if seed.empty:
        lines.append(f"пустых вне описи {seed.empty} — затравка их не судит")
    if seed.outside:
        lines.append(f"вне корней по истории: {seed.outside} (харнес/) — не снимаются")
    return lines


def judge_seed(ready: Prepared, project: str, history: PackageHistory, selftest: bool):
    """Затравка по истории пакета: (строки для печати, согласованные {путь: blob}).
    Неизвестные файлы ищутся, только если опись читаема: иначе все её строки стали бы чужими."""
    prior = ready.prior
    if prior.broken:
        return ([] if history.root == "-" else ["затравки нет: опись нечитаема"]), {}
    known = {e.rel for e in ready.entries} | set(ready.unplaced)
    if prior.parsed is not None:
        known |= set(prior.parsed.body) | set(prior.parsed.kept) | set(prior.parsed.deferred)
    unknown = layout_history.unknown_files(project, known, ready.absent)
    if not unknown:
        return [], {}
    found = history.get()
    if isinstance(found, layout_history.NoHistory):
        return [f"затравки нет: {found.reason}"], {}
    seed = layout_history.seed_verdict(project, unknown, found)
    lines, approved = [], {}
    if seed.would_remove:
        approved, lines = seed_consent(seed.would_remove, selftest)
    waiting = sorted(set(seed.would_remove) - set(approved))
    if waiting:
        lines.insert(0, f"по истории пакета снял бы {len(waiting)}: {', '.join(waiting)}; "
                        "ждёт «да, убрать»")
        lines.insert(1, f"{SEED_LIST_PREFIX}{json.dumps(waiting, ensure_ascii=False)}")
    return lines + seed_summary(seed, found.shallow), approved


def row_blob(parsed, rel: str) -> str:
    """blob строки описи: из тела, а строка «оставлено», ставшая кандидатом, — оттуда."""
    return (parsed.body[rel] if rel in parsed.body else parsed.kept[rel])[0]


def pass_one(plan_raw: bytes, project: str, selftest: bool, lift: str,
             rollback_of: str = "", history: PackageHistory | None = None) -> Prepared:
    """Ход 1: в проект не пишет."""
    ready = Prepared()
    history = history or PackageHistory("-")
    project_real = os.path.realpath(project)
    troubles = ready.troubles
    ready.entries = [e for e in (classify(r, project, project_real, troubles, ready.absent,
                                          ready.unplaced)
                                 for r in parse_plan(plan_raw)) if e is not None]
    entries = ready.entries
    check_duplicates(entries, troubles)
    ready.notes = check_remnants([e.dst for e in entries], troubles)
    explicit = os.environ.get("HARNESS_LAYOUT_ARCHIVE")
    if rollback_of:
        # Судить о лишнем по описи и порогу здесь нечем: снимается ровно то, что назвал журнал.
        ready.rollback = judge_rollback(rollback_of, entries, project, troubles, ready.notes)
        if ready.rollback is not None:
            ready.removals = Removals(sorted(ready.rollback.decisions.remove.items()))
        archive_needed = True       # шапка «откат» отличает раскладчика с Ф-г от прежнего
        if history.root != "-":
            ready.seed_lines = ["затравки нет: режим отката"]
    else:
        ready.prior = read_prior(project)
        own = history.own() if ready.prior.parsed is not None else frozenset()
        unchecked = own.reason if isinstance(own, layout_history.NoHistory) else ""
        ready.verdict, ready.removals = judge_prior(
            ready.prior, entries, ready.absent, project, lift, history_ours(history, project),
            frozenset() if unchecked else own)
        if unchecked and ready.removals.rows:
            ready.notes.append(f"своё копии не проверено: {unchecked}")
        ready.seed_lines, approved = judge_seed(ready, project, history, selftest)
        ready.removals.add_seeded(approved)
        archive_needed = bool(ready.removals.rows) or any(
            e.needs_copy or (explicit and e.writes) for e in entries)
    # Беды архива считаются вместе с остальными (все беды списком), а каталог и шапка
    # журнала создаются, только когда бед нет: журнал без «итог» читается как оборванный прогон.
    if archive_needed:
        overwrites = [e for e in entries if e.needs_copy]
        path = plan_archive(overwrites, ready.removals.paths, selftest, troubles)
        if path is not None and not troubles:
            ready.archive = open_archive(path, entries, overwrites, ready.removals.paths,
                                         lift, troubles, rollback_of)
    prior = ready.prior
    if prior is not None and prior.copyable and (prior.broken or explicit) and not troubles:
        ready.archive = shelter_prior(prior, ready.archive, entries, selftest)
        if not prior.broken and not prior.sheltered:
            ready.notes.append(f"копия прежней описи не снята: {prior.unavailable}")
    return ready


def refuse(troubles: list) -> int:
    print(MARKER + troubles[0].short + (f" (и ещё бед: {len(troubles) - 1})"
                                        if len(troubles) > 1 else ""), file=sys.stderr)
    for trouble in troubles:
        for line in trouble.details:
            print(f"  - {line}", file=sys.stderr)
    return EXIT_REFUSED


def write_temp(src: str, tmp: str, mode: int) -> None:
    # O_NOFOLLOW: остаток под зарезервированным именем не уводит запись по ссылке.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
        shutil.copyfileobj(source, out)
        out.flush()
        os.fchmod(out.fileno(), mode)
        os.fsync(out.fileno())


class Stop(Exception):
    """Стоп хода 2: дальше ни одной записи."""


def make_copy(entry: Entry, archive: Archive) -> None:
    final = os.path.join(archive.path, "files", entry.rel)
    temp = os.path.join(os.path.dirname(final), f".{os.path.basename(final)}.copy-tmp")
    copy_blob = copy_to_archive(entry.dst, temp)
    if copy_blob != entry.blob_old:
        os.unlink(temp)
        raise Stop(f"копия не совпала с файлом: {entry.rel}")
    os.replace(temp, final)


def lay_file(entry: Entry, archive: Archive | None, kill_after: int, written: list) -> None:
    journaled = archive is not None and entry.group != REBUILT_GROUP
    if journaled and entry.state == "differs":
        archive.log("перекрыт", entry.blob_old, entry.blob_new, entry.rel)
    elif journaled:
        archive.log("положен-новый", "-", entry.blob_new, entry.rel)
    if entry.needs_copy:
        make_copy(entry, archive)
    os.makedirs(os.path.dirname(entry.dst), exist_ok=True)
    temp = os.path.join(os.path.dirname(entry.dst), f".{os.path.basename(entry.dst)}.layout-tmp")
    write_temp(entry.src, temp, entry.mode)
    if blob(temp) != entry.blob_new:
        os.unlink(temp)
        raise Stop(f"источник изменился во время раскладки: {entry.rel}")
    os.replace(temp, entry.dst)
    written.append(entry.rel)
    if kill_after and len(written) == kill_after:
        os.kill(os.getpid(), signal.SIGKILL)
    if journaled:
        archive.log("готово", "-", "-", entry.rel)


def move_to_archive(src: str, final: str, row_blob: str) -> None:
    """Перенос, а не копия + rm: пока файл не лежит в архиве сверенным, исходник цел."""
    os.makedirs(os.path.dirname(final), exist_ok=True)
    try:
        os.rename(src, final)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    temp = os.path.join(os.path.dirname(final), f".{os.path.basename(final)}.copy-tmp")
    if copy_to_archive(src, temp) != row_blob:
        os.unlink(temp)
        raise Stop(f"копия не совпала с файлом: {src}")
    os.replace(temp, final)
    os.unlink(src)


def prune_empty_dirs(project: str, rel: str) -> None:
    """Пустые каталоги вверх от снятого файла до корня укладки (первый компонент пути)."""
    parts = rel.split("/")[:-1]
    while len(parts) > 1:
        try:
            os.rmdir(os.path.join(project, *parts))
        except OSError:
            return
        parts.pop()


def remove_extra(removals: Removals, archive: Archive, project: str, kill_after: int,
                 written: list) -> None:
    project_real = os.path.realpath(project)
    for rel, row in removals.rows:
        # Ход 1 судил раньше: между ходами файл правят и подменяют каталог ссылкой.
        why = layout_manifest.disk_state(project, project_real, rel, row)
        if why:
            removals.skipped[rel] = why
            continue
        if rel in removals.seeded:
            write_journal_line(archive.fd, SEED_MARK + rel)
        archive.log("снят", row, "-", rel)
        final = os.path.join(archive.path, "files", rel)
        move_to_archive(os.path.join(project, rel), final, row)
        if blob(final) != row:
            raise Stop(f"снятый файл изменился во время раскладки: {rel} — он в архиве {final}")
        written.append(rel)
        if kill_after and len(written) == kill_after:
            os.kill(os.getpid(), signal.SIGKILL)
        archive.log("готово", "-", "-", rel)
        removals.done.add(rel)
        prune_empty_dirs(project, rel)


def restore_file(rollback: Rollback, rel: str, want: str, archive: Archive, project: str,
                 kill_after: int, written: list) -> None:
    """Возврат своего файла из копии прямого прогона. Пишет только на то, что видел суд хода 1:
    чужое на пути — «занято», а не перезапись."""
    src, dst = os.path.join(rollback.files, rel), os.path.join(project, rel)
    expected = rollback.expected[rel]
    if blob(src) != want:
        raise Stop(f"копия в архиве прямого прогона изменилась: {rel}")
    archive.log("возвращён", expected or "-", want, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    temp = os.path.join(os.path.dirname(dst), f".{os.path.basename(dst)}.layout-tmp")
    write_temp(src, temp, stat.S_IMODE(os.stat(src).st_mode))
    if blob(temp) != want:
        os.unlink(temp)
        raise Stop(f"копия изменилась во время возврата: {rel}")
    if project_blob(project, os.path.realpath(project), rel) != expected:
        os.unlink(temp)
        rollback.kept[rel] = OCCUPIED
        return
    os.replace(temp, dst)
    written.append(rel)
    if kill_after and len(written) == kill_after:
        os.kill(os.getpid(), signal.SIGKILL)
    archive.log("готово", "-", "-", rel)
    rollback.restored.add(rel)


def record_kept(rollback: Rollback, removals: Removals, archive: Archive) -> None:
    """Всё, что откат оставил, — в журнал отката по одной строке с причиной."""
    for rel, why in removals.skipped.items():
        rollback.kept[rel] = KEPT_WORDS.get(why, why)
    for rel, reason in sorted(rollback.kept.items(), key=lambda item: item[0].encode("utf-8")):
        write_journal_line(archive.fd, f"{layout_journal.KEPT_PREFIX}{reason}\t{rel}")


def pass_two(ready: Prepared, project: str, kill_after: int) -> int:
    entries, archive = ready.entries, ready.archive
    written: list = []
    try:
        for entry in entries:
            if entry.writes:
                lay_file(entry, archive, kill_after, written)
        remove_extra(ready.removals, archive, project, kill_after, written)
        if ready.rollback is not None:
            for rel, want in sorted(ready.rollback.decisions.restore.items()):
                restore_file(ready.rollback, rel, want, archive, project, kill_after, written)
            record_kept(ready.rollback, ready.removals, archive)
        if archive is not None:
            write_journal_line(archive.fd, f"итог\t-\t-\t{archive.actions}")
    except (Stop, OSError) as failure:
        print(f"дверь укладки: сбой середины, дальше не пишу: {failure}", file=sys.stderr)
        return EXIT_FAILED
    overwritten = sum(1 for e in entries if e.needs_copy)
    fresh = sum(1 for e in entries if e.state == "new")
    untouched = sum(1 for e in entries if e.state == "same")
    where = f", архив {archive.path}" if archive is not None else ""
    print(f"дверь укладки: перекрыто {overwritten}, новых {fresh}, не тронуто {untouched}{where}")
    return 0


def removal_limit() -> str | None:
    """Сырое значение порога, %; None — ключа нет. Число ли оно, судит модуль описи."""
    return config.конфиг(config.путь_harness_conf()).get(LIMIT_KEY, "").strip() or None


def read_crontab() -> tuple:
    """(текст, причина отказа): «нет расписания» — это пустой текст, а не отказ."""
    try:
        done = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return "", str(error)
    if done.returncode == 0:
        return done.stdout, ""
    if done.returncode == 1 and "no crontab" in done.stderr:
        return "", ""
    return "", (done.stderr.strip().splitlines() or [f"код {done.returncode}"])[0]


def schedule_lines(removed: list, project: str) -> list:
    """Строки расписания пользователя, где назван снятый файл: он перестал существовать."""
    if not removed:
        return []
    text, refusal = read_crontab()
    if refusal:
        return [f"расписание не прочитано: {refusal}"]
    roots = {project, os.path.realpath(project)}
    return [f"в расписании строка на снятый файл: {rel}" for rel in removed
            if any(re.search(re.escape(os.path.join(root, rel)) + r"(?![\w./-])", line)
                   for root in roots for line in text.splitlines())]


def after_removal_lines(removals: Removals, project: str, lift: str) -> list:
    lines = []
    done = sorted(removals.done)
    if lift and set(done) - removals.seeded:
        lines.append(f"снято сверх порога по доводу: {lift}")
    by_history = [rel for rel in done if rel in removals.seeded]
    if by_history:
        lines.append(f"снято по истории пакета: {', '.join(by_history)}")
    lines += [f"не снято: {rel} — {why}" for rel, why in sorted(removals.skipped.items())]
    return lines + schedule_lines(done, project)


def manifest_plan(entries: list) -> set:
    """Что попадёт в тело описи: (путь, blob, группа) без пересобираемой группы и яруса 1."""
    return {(e.rel, e.blob_new, e.group) for e in entries
            if e.group != REBUILT_GROUP and layout_manifest.forbidden_tier(e.rel) != 1}


def judge_manifest(ready: Prepared, overwritten: int, package: str):
    """Новая опись и строки для печати; None вместо описи — прежнюю не трогать."""
    prior, plan = ready.prior, manifest_plan(ready.entries)
    if prior.raw is None:
        manifest = layout_manifest.Manifest(package, {p: (b, g) for p, b, g in plan}, {})
        return manifest, [f"опись заведена: {len(plan)} строк; "
                          "лишнее начну считать со следующей раскладки"]
    if prior.broken and not prior.sheltered:
        why = (f"архив недоступен ({prior.unavailable})" if prior.copyable
               else "копию снять нельзя")
        return None, [f"опись нечитаема ({prior.reason}), {why} — прежняя опись оставлена"]
    if prior.broken:
        manifest = layout_manifest.Manifest(package, {p: (b, g) for p, b, g in plan}, {})
        return manifest, [f"опись нечитаема ({prior.reason}), копия: {prior.sheltered}",
                          f"опись заведена: {len(plan)} строк; "
                          "лишнее начну считать со следующей раскладки"]
    verdict, removed = ready.verdict, ready.removals.done
    manifest = layout_manifest.Manifest(package, verdict.body_after(removed),
                                        verdict.kept_after(ready.removals.skipped),
                                        verdict.deferred_after())
    return manifest, verdict.summary_lines(removed - ready.removals.seeded, overwritten)


def write_manifest(project: str, data: bytes) -> None:
    """Временный файл с зарезервированным именем и os.replace: прежняя опись цела при сбое."""
    target = os.path.join(project, layout_manifest.MANIFEST_NAME)
    temp = os.path.join(project, f".{layout_manifest.MANIFEST_NAME}.layout-tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, target)
    except OSError as error:
        # Файлы уже легли: код 1 запустил бы откат обновления из-за одной описи.
        try:
            os.unlink(temp)
        except OSError:
            pass
        print(f"опись не записана: {os.strerror(error.errno)} — прежняя опись оставлена")


def settle_manifest(ready: Prepared, package: str, project: str, after: list) -> None:
    overwritten = sum(1 for e in ready.entries if e.needs_copy)
    manifest, lines = judge_manifest(ready, overwritten, package)
    for line in lines + after:
        print(line)
    if manifest is None:
        return
    text = layout_manifest.render(manifest)
    if ready.prior.raw != text.encode("utf-8"):
        write_manifest(project, text.encode("utf-8"))


def rollback_line(rollback: Rollback, removals: Removals) -> str:
    kept = sorted(rollback.kept, key=lambda rel: rel.encode("utf-8"))
    return (f"откат по журналу {rollback.given}: снято {len(removals.done)}, "
            f"возвращено {len(rollback.restored)}, оставлено {len(kept)}"
            + (": " + ", ".join(kept) if kept else "")
            + f"; строк без «готово» {rollback.decisions.undone}")


def left_behind_rows(rollback: Rollback, project: str) -> dict:
    """Новые файлы версии, правленные после обновления: откат их оставил, и из описи они не должны
    выпасть (иначе ни одна сводка о них больше не скажет)."""
    rows = {}
    for rel, reason in rollback.kept.items():
        if reason == "правлен после обновления":
            current = project_blob(project, os.path.realpath(project), rel)
            if current not in (None, layout_journal.NOT_A_FILE):
                rows[rel] = (current, "правлен здесь")
    return rows


def prior_copy(rollback: Rollback) -> bytes | None:
    """Прежняя опись из копии прямого прогона, если журнал на неё указывает и blob сошёлся."""
    wanted = rollback.journal.manifest_copy
    if not wanted:
        return None
    copy = os.path.join(rollback.files, layout_manifest.MANIFEST_NAME)
    try:
        # lstat до open: FIFO без писателя вешает open, ссылка на /dev/zero читается вечно.
        # Размер судится по lstat ДО чтения: разреженный файл больше предела
        # иначе читался бы целиком, пока не дойдёт до проверки.
        info = os.lstat(copy)
        if not stat.S_ISREG(info.st_mode) or info.st_size > layout_journal.JOURNAL_LIMIT:
            return None
        with open(copy, "rb") as handle:
            raw = handle.read(layout_journal.JOURNAL_LIMIT + 1)
    except OSError:
        return None
    if len(raw) > layout_journal.JOURNAL_LIMIT:
        return None
    return raw if blob_of_bytes(raw) == wanted else None


def rollback_manifest(rollback: Rollback, ready: Prepared, package: str, project: str):
    """(байты описи или None — не трогать, строка для печати). Опись после отката — опись ДО
    обновления; прежней не было — опись по плану A, как первая."""
    if not rollback.journal_exists:
        return None, ""
    extra = left_behind_rows(rollback, project)
    raw = prior_copy(rollback)
    if raw is not None:
        parsed = layout_manifest.parse_bytes(raw)
        fresh = {rel: row for rel, row in extra.items() if isinstance(parsed, layout_manifest.Manifest)
                 and rel not in parsed.body and rel not in parsed.kept
                 and rel not in parsed.deferred}
        if fresh:
            parsed.kept.update(fresh)
            raw = layout_manifest.render(parsed).encode("utf-8")
        return raw, "опись возвращена к состоянию до обновления"
    plan = manifest_plan(ready.entries)
    manifest = layout_manifest.Manifest(package, {p: (b, g) for p, b, g in plan}, extra)
    missing = rollback.journal.manifest_missing
    why = (f"копия описи до обновления не снята: {missing}" if missing
           else "прежней описи в архиве нет")
    return (layout_manifest.render(manifest).encode("utf-8"),
            f"{why}: опись заведена по плану A, {len(plan)} строк")


def settle_rollback_manifest(rollback: Rollback, ready: Prepared, package: str, project: str) -> None:
    data, line = rollback_manifest(rollback, ready, package, project)
    if data is None:
        return
    print(line)
    target = os.path.join(project, layout_manifest.MANIFEST_NAME)
    try:
        current = Path(target).read_bytes()
    except OSError:
        current = None
    if current != data:
        write_manifest(project, data)


def append_resolved(journal_path: str, by: str) -> None:
    """«Разобран» в конец чужого журнала. Недописанная последняя строка (убитый прогон) без
    этого склеилась бы с новой: её байты остаются, но комментарием."""
    try:
        with open(journal_path, "rb") as handle:
            data = handle.read()
        cut = data.rfind(b"\n") + 1
        fd = os.open(journal_path, os.O_WRONLY | os.O_APPEND)
        try:
            if cut < len(data):
                os.ftruncate(fd, cut)
                tail = data[cut:].decode("utf-8", "replace")
                write_journal_line(fd, f"# оборвана запись: {tail}")
            write_journal_line(fd, f"разобран\t-\t-\t{by}")
        finally:
            os.close(fd)
    except OSError as error:
        print(f"разобран не дописан: {journal_path}: {os.strerror(error.errno) if error.errno else error}")


def read_orphans(by: str, exclude: set, forward: str, selftest: bool) -> None:
    """Оборванные журналы своего проекта называются: следующая раскладка — единственная, кто про
    них скажет. Код прогона это не меняет."""
    try:
        backups = config.backup_dir()
    except LookupError as refusal:
        print(f"оборванные журналы не искал: {refusal}")
        return
    if selftest and not selftest_target_ok(backups):
        print(f"оборванные журналы не искал: самотест целится в живой каталог копий: {backups}")
        return
    install_conf = config.путь_install_conf()
    name = config.конфиг(install_conf).get("PROJECT_NAME", "")
    if not name:
        print(f"оборванные журналы не искал: PROJECT_NAME пуст в {install_conf}")
        return
    for archive, undone, reason in layout_journal.find_orphans(str(backups / "раскладка"), name, exclude):
        if undone is None:
            print(f"журнал не прочитан: {archive}: {reason}")
            continue
        print(f"оборванная раскладка {archive}: {undone} строк без «готово»")
        append_resolved(os.path.join(archive, "journal.tsv"), by)
    if forward:
        journal, _ = layout_journal.read_journal(forward)
        if journal is not None and not journal.has_total:
            append_resolved(os.path.join(forward, "journal.tsv"), by)


def lift_troubles(argv: list, options: dict) -> list:
    """Довод снятия сверх порога попадает в журнал строкой: пустая причина или перевод
    строки в ней ломают формат, а довод без значения dict(zip) выбросил бы молча."""
    found = []
    if len(argv) % 2:
        found.append(Trouble(f"довод без значения: {argv[-1]}"))
    if LIFT_ARG in options:
        reason = options[LIFT_ARG]
        if not reason.strip():
            found.append(Trouble(f"{LIFT_ARG} без причины"))
        elif "\n" in reason or "\t" in reason:
            found.append(Trouble(f"{LIFT_ARG}: причина с переводом строки или табом"))
    if ROLLBACK_ARG in options and not options[ROLLBACK_ARG].strip():
        found.append(Trouble(f"{ROLLBACK_ARG} без пути"))
    if ROLLBACK_ARG in options and LIFT_ARG in options:
        found.append(Trouble(f"{ROLLBACK_ARG} и {LIFT_ARG} несовместимы: у отката порога нет"))
    return found


def parse_args(argv: list) -> tuple:
    options = dict(zip(argv[0::2], argv[1::2]))
    commit = options.get("--package-commit", "-")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        commit = "-"
    return (options["--plan"], options["--project"], commit, options.get(LIFT_ARG, ""),
            options.get(ROLLBACK_ARG, ""), lift_troubles(argv, options),
            options.get(PACKAGE_ROOT_ARG, "-"))


def own_archive(ready: Prepared) -> str:
    """Что «разобран» называет: архив этого прогона, даже если каталог не создан."""
    return os.environ.get("HARNESS_LAYOUT_ARCHIVE") or (ready.archive.path if ready.archive else "-")


def exclusions(ready: Prepared, forward: str) -> set:
    return {path for path in (os.environ.get("HARNESS_LAYOUT_ARCHIVE"), forward,
                              ready.archive.path if ready.archive else "") if path}


def finish_rollback(ready: Prepared, package: str, project: str, selftest: bool) -> int:
    rollback = ready.rollback
    print(rollback_line(rollback, ready.removals))
    settle_rollback_manifest(rollback, ready, package, project)
    occupied = sorted(rel for rel, why in rollback.kept.items() if why == OCCUPIED)
    if occupied:
        print("дверь укладки: не возвращено, путь занят: " + ", ".join(occupied), file=sys.stderr)
        return EXIT_FAILED
    read_orphans(own_archive(ready), exclusions(ready, rollback.given), rollback.given, selftest)
    return 0


def usage_wanted(argv: list) -> bool:
    given = dict(zip(argv[0::2], argv[1::2]))
    return bool(HELP_ARGS & set(argv)) or any(name not in given for name in REQUIRED_ARGS)


def main(argv: list) -> int:
    if usage_wanted(argv):
        print(__doc__)
        return EXIT_USAGE
    plan_file, project, package, lift, rollback_of, bad_args, package_root = parse_args(argv)
    if bad_args:
        return refuse(bad_args)
    selftest = os.environ.get("HARNESS_SELFTEST") == "1"
    kill_after = 0
    for name in SELFTEST_DOORS:
        if os.environ.get(name) and not selftest:
            print(f"{name} вне самотеста — не действует")
    if selftest and os.environ.get("HARNESS_LAYOUT_KILL_AFTER_WRITE"):
        kill_after = int(os.environ["HARNESS_LAYOUT_KILL_AFTER_WRITE"])
    plan_raw = Path(plan_file).read_bytes()
    ready = pass_one(plan_raw, project, selftest, lift, rollback_of, PackageHistory(package_root))
    if ready.troubles:
        return refuse(ready.troubles)
    for note in ready.notes + ready.seed_lines:
        print(note)
    plan_out = os.environ.get("HARNESS_LAYOUT_PLAN_OUT") if selftest else None
    if plan_out:
        Path(plan_out).write_bytes(b"".join(
            f"{e.group}\t{e.src}\t{e.rel}\0".encode("utf-8") for e in ready.entries))
    code = pass_two(ready, project, kill_after)
    if code != 0:
        return code
    if ready.rollback is not None:
        return finish_rollback(ready, package, project, selftest)
    settle_manifest(ready, package, project, after_removal_lines(ready.removals, project, lift))
    read_orphans(own_archive(ready), exclusions(ready, ""), "", selftest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
