"""Опись раскладки: что дверь укладки положила в проект и что из этого уже не в пакете.

Опись — файл <PROJECT_DIR>/.layout-manifest. Модуль разбирает его, пишет и судит
«лишнее» (файл есть в прежней описи, а в плане новой раскладки его нет). Снимать
файлы модуль не умеет: он называет кандидатов и откладывает их по порогу, а снимает
дверь укладки; сводку и тело новой описи модуль считает по тому, что дверь СНЯЛА.

Формат (UTF-8, перевод строки \\n):
    # layout-manifest 2
    # пакет <40 hex коммита пакета | ->
    <blob40>\\t<группа>\\t<путь>        тело, сортировка по байтам UTF-8
    # оставлено
    <blob40>\\t<причина>\\t<путь>
    # отложено                         необязательная секция, только непустая
    <путь>                             отложен порогом или ключом: довод снимает ровно их
"""
import fnmatch
import hashlib
import math
import os
import re
import stat

MANIFEST_NAME = ".layout-manifest"
VERSION_LINE = "# layout-manifest 2"
PACKAGE_PREFIX = "# пакет "
KEPT_HEADER = "# оставлено"
DEFERRED_HEADER = "# отложено"
KEPT_REASONS = ("правлен здесь", "ссылка", "не файл", "за проектом")
NOT_A_FILE = "не файл"
REASON_THRESHOLD = "порог"
REASON_NO_KEY = "ключ не задан"
REASON_BAD_KEY = "порог не число"
REASON_NO_SOURCE = "группа без источника"
REASON_GONE = "уже не было"
_LIFTABLE_REASONS = (REASON_THRESHOLD, REASON_NO_KEY, REASON_BAD_KEY)

_BLOB = re.compile(r"[0-9a-f]{40}\Z")
_PACKAGE = re.compile(r"(-|[0-9a-f]{40})\Z")

# Файлы, которые раскладка кладёт: корни укладки. Всё вне них — не её дело,
# поэтому опись такие пути не знает и не снимает.
LAYOUT_ROOTS = ("scripts/", "harness/", "UNIFIED/", ".claude/agents/")

# Ярус 1: не входит в опись и не снимается. Живые записи этой установки
# (состояние, память, настройки конвейера) и то, что пересобирает генератор.
FORBIDDEN_NOT_LISTED = (
    "STATE.md", "dev-map.yaml", "CLAUDE.md", ".gitignore",
    "память/*", "docs/*", ".tasks/*", ".git/*",
    "harness/skills/*",
    "*/.установлено",
    "*.layout-tmp",
    MANIFEST_NAME,
    "harness/config/pipeline.yaml", "harness/config/queue.yaml",
    "harness/config/models.yaml", "harness/config/audit.yaml",
    "harness/config/live-check-state.yaml",
)

# Ярус 2: в описи есть, заменяется раскладкой, но не снимается. Опись нужна, чтобы
# знать, что файл наш; снять его нельзя, потому что неполная сборка пакета без
# project-settings.json или без хука иначе сняла бы стражей — а они держат И-1/И-2.
FORBIDDEN_LISTED = (".claude/settings.json", "scripts/hooks/*")


def _matches(path: str, masks: tuple) -> bool:
    return any(fnmatch.fnmatchcase(path, mask) for mask in masks)


def forbidden_tier(path: str) -> int:
    """0 — обычный путь; 1 — не в описи и не снимается; 2 — в описи, не снимается."""
    if _matches(path, FORBIDDEN_NOT_LISTED):
        return 1
    if path != ".claude/settings.json" and not path.startswith(LAYOUT_ROOTS):
        return 1
    return 2 if _matches(path, FORBIDDEN_LISTED) else 0


def listable(path: str) -> str | None:
    """Причина, по которой путь нельзя записать в опись, или None."""
    if not path:
        return "пустой путь"
    if "\t" in path or "\n" in path:
        return "таб или перевод строки в пути"
    if path.startswith("/"):
        return "абсолютный путь"
    if ".." in path.split("/"):
        return "компонент «..»"
    return None


class ManifestBroken:
    """Опись, которую нельзя читать: пустой Manifest из неё не получается никогда."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:
        return f"ManifestBroken({self.reason!r})"


class Manifest:
    def __init__(self, package: str, body: dict, kept: dict, deferred=()) -> None:
        self.package = package
        self.body = body
        self.kept = kept
        self.deferred = frozenset(deferred)


def _row(line: str) -> tuple | str:
    """(blob, середина, путь) или причина отказа."""
    fields = line.split("\t")
    if len(fields) != 3:
        return f"строка не из трёх полей: {line!r}"
    if not _BLOB.match(fields[0]):
        return f"blob не из 40 знаков hex: {line!r}"
    if not fields[1] or not fields[2]:
        return f"пустое поле в строке: {line!r}"
    return tuple(fields)


def _take_rows(lines: list, kept_section: bool, seen: set) -> dict | ManifestBroken:
    rows = {}
    for line in lines:
        row = _row(line)
        if isinstance(row, str):
            return ManifestBroken(row)
        blob, middle, path = row
        if kept_section and middle not in KEPT_REASONS:
            return ManifestBroken(f"причина «оставлено» вне допустимых: {line!r}")
        if path in seen:
            return ManifestBroken(f"путь повторён: {path}")
        seen.add(path)
        rows[path] = (blob, middle)
    return rows


def parse(text: str) -> Manifest | ManifestBroken:
    lines = text.split("\n")
    if lines[-1] != "":
        return ManifestBroken("файл не кончается переводом строки (или пуст)")
    lines.pop()
    if not lines or lines[0] != VERSION_LINE:
        first = lines[0] if lines else ""
        return ManifestBroken(f"версия не {VERSION_LINE!r}: {first!r}")
    if len(lines) < 2 or not lines[1].startswith(PACKAGE_PREFIX):
        return ManifestBroken("нет строки «# пакет …»")
    package = lines[1][len(PACKAGE_PREFIX):]
    if not _PACKAGE.match(package):
        return ManifestBroken(f"строка пакета не «-» и не 40 знаков hex: {package!r}")
    rest = lines[2:]
    if KEPT_HEADER not in rest:
        return ManifestBroken(f"нет секции «{KEPT_HEADER}»")
    cut = rest.index(KEPT_HEADER)
    seen: set = set()
    body = _take_rows(rest[:cut], False, seen)
    if isinstance(body, ManifestBroken):
        return body
    tail = rest[cut + 1:]
    deferred_lines: list = []
    has_deferred = DEFERRED_HEADER in tail
    if has_deferred:
        at = tail.index(DEFERRED_HEADER)
        tail, deferred_lines = tail[:at], tail[at + 1:]
    kept = _take_rows(tail, True, seen)
    if isinstance(kept, ManifestBroken):
        return kept
    deferred = set(deferred_lines)
    if has_deferred and (len(deferred) != len(deferred_lines) or not deferred):
        return ManifestBroken(f"секция «{DEFERRED_HEADER}» пуста или с повтором")
    stray = sorted(p for p in deferred if p not in body and p not in kept)
    if stray:
        return ManifestBroken(f"отложен путь не из описи: {stray[0]}")
    return Manifest(package, body, kept, deferred)


def parse_bytes(raw: bytes) -> Manifest | ManifestBroken:
    try:
        return parse(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        return ManifestBroken(f"не UTF-8: {error.reason}")


def _by_bytes(item: tuple) -> bytes:
    return item[0].encode("utf-8")


def render(manifest: Manifest) -> str:
    """Один и тот же вход — одни и те же байты: ни времени, ни пути архива."""
    lines = [VERSION_LINE, PACKAGE_PREFIX + manifest.package]
    lines += [f"{blob}\t{group}\t{path}"
              for path, (blob, group) in sorted(manifest.body.items(), key=_by_bytes)]
    lines.append(KEPT_HEADER)
    lines += [f"{blob}\t{reason}\t{path}"
              for path, (blob, reason) in sorted(manifest.kept.items(), key=_by_bytes)]
    if manifest.deferred:
        lines.append(DEFERRED_HEADER)
        lines += sorted(manifest.deferred, key=lambda p: p.encode("utf-8"))
    return "\n".join(lines) + "\n"


def _files_word(count: int) -> str:
    tail = count % 100
    if tail % 10 == 1 and tail != 11:
        return "файл"
    if 2 <= tail % 10 <= 4 and not 12 <= tail <= 14:
        return "файла"
    return "файлов"


def _joined(paths) -> str:
    return ", ".join(sorted(paths, key=lambda p: p.encode("utf-8")))


class Verdict:
    def __init__(self) -> None:
        self.old_rows = 0
        self.percent = None
        self.would_remove: set = set()
        self.deferred: dict = {}
        self.kept: dict = {}
        self.gone: set = set()
        self.broken: set = set()
        self.not_removable: set = set()
        self.new_body: dict = {}
        self.new_kept: dict = {}
        self.revived: dict = {}
        self.seeded: dict = {}
        self.threshold_line = ""
        self.absent_lines: list = []

    def body_after(self, removed: set) -> dict:
        """Тело новой описи без путей, которые дверь действительно сняла: оборвавшаяся
        раскладка не теряет строк, а отложенное и ярус 2 остаются на месте."""
        return {path: row for path, row in self.new_body.items() if path not in removed}

    def kept_after(self, skipped: dict) -> dict:
        """«Оставлено» новой описи. Кандидат из «оставлено», которого дверь не сняла, туда
        возвращается с причиной перепроверки: иначе он не значился бы нигде. Причина вне
        словаря «оставлено» (файла уже нет) строки не даёт."""
        kept = dict(self.new_kept)
        for path, row_blob in self.revived.items():
            if skipped.get(path) in KEPT_REASONS:
                kept[path] = (row_blob, skipped[path])
        return kept

    def deferred_after(self) -> set:
        """Что довод снятия сверх порога снимет в следующий раз: отложенное порогом или ключом."""
        return {path for path, reason in self.deferred.items()
                if reason.startswith(_LIFTABLE_REASONS)}

    # Умолчания пусты: update.py зовёт без аргументов, у него снятия и перекрытия нет.
    def summary_lines(self, removed: set = frozenset(), overwritten: int = 0) -> list:
        removed_text = f"снято {len(removed)}" + (f": {_joined(removed)}" if removed else "")
        reasons: dict = {}
        for reason in self.deferred.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        deferred = f"отложено {len(self.deferred)}" + (
            " (" + ", ".join(f"{r} {n}" for r, n in sorted(reasons.items())) + ")"
            if reasons else "")
        kept = f"оставлено {len(self.kept)}" + (f": {_joined(self.kept)}" if self.kept else "")
        lines = [f"опись: в описи {self.old_rows}; {removed_text}; перекрыто {overwritten}; "
                 f"{deferred}; {kept}; уже не было {len(self.gone)}"]
        lines += [f"опись испорчена: {p}" for p in sorted(self.broken)]
        lines += [f"не снимается: {p}" for p in sorted(self.not_removable)]
        lines += [f"уже не было: {p}" for p in sorted(self.gone)]
        lines += self.absent_lines
        if self.threshold_line:
            lines.append(self.threshold_line)
        no_key = sum(1 for reason in self.deferred.values() if reason == REASON_NO_KEY)
        if no_key:
            lines.append(f"ключ LAYOUT_REMOVE_MAX_PERCENT не задан — отложено {no_key}")
        if self.percent is not None and _percent_limit(self.percent)[1]:
            lines.append(f"ключ LAYOUT_REMOVE_MAX_PERCENT не число: {self.percent}")
        return lines


def git_blob(path: str) -> str:
    """То же, что `git hash-object <path>`, без git; файл читается потоком."""
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        digest.update(b"blob %d\0" % os.fstat(handle.fileno()).st_size)
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def chain_is_honest(project_real: str, rel: str) -> bool:
    """Каждый каталог цепочки настоящий: ссылка-каталог увела бы запись в другой путь."""
    parent = os.path.realpath(os.path.dirname(os.path.join(project_real, rel)))
    return parent == os.path.normpath(os.path.join(project_real, os.path.dirname(rel)))


def is_plain_file(project: str, project_real: str, rel: str) -> bool:
    """Обычный файл по честной цепочке; содержимое не читается."""
    if not chain_is_honest(project_real, rel):
        return False
    try:
        return stat.S_ISREG(os.lstat(os.path.join(project, rel)).st_mode)
    except OSError:
        return False


def project_blob(project: str, project_real: str, rel: str):
    """Единственный судья «что лежит в проекте по пути»: blob обычного файла; None — пути
    нет; NOT_A_FILE — занято иным, ссылкой или цепочкой через ссылку."""
    if not chain_is_honest(project_real, rel):
        return NOT_A_FILE
    full = os.path.join(project, rel)
    try:
        if not stat.S_ISREG(os.lstat(full).st_mode):
            return NOT_A_FILE
        return git_blob(full)
    except FileNotFoundError:
        return None
    except OSError:
        return NOT_A_FILE


def disk_state(project: str, project_real: str, path: str, row_blob: str) -> str | None:
    """Причина «оставить» или None — можно снимать. Чужую цель не читаем. Единственный
    судья пути: суд описи зовёт его при выборе кандидатов, дверь — перед каждым переносом."""
    full = os.path.join(project, path)
    try:
        mode = os.lstat(full).st_mode
    except FileNotFoundError:
        return REASON_GONE
    except OSError:
        return "правлен здесь"
    if stat.S_ISLNK(mode):
        return "ссылка"
    if not stat.S_ISREG(mode):
        return NOT_A_FILE
    # Каждый каталог цепочки обязан быть настоящим: ссылка-каталог ВНУТРИ проекта
    # (scripts/sub -> real) вела бы rename в другой путь, чем записан в описи.
    if not chain_is_honest(project_real, path):
        parent = os.path.realpath(os.path.dirname(full))
        inside = parent == project_real or parent.startswith(project_real + os.sep)
        return "ссылка" if inside else "за проектом"
    try:
        disk_blob = git_blob(full)
    except OSError:
        return "правлен здесь"      # не прочли — не докажем, что файл наш
    return None if disk_blob == row_blob else "правлен здесь"


def absent_dest(path: str, absent: list) -> str | None:
    for item in absent:
        dest = item[1] if isinstance(item, tuple) else item
        if path == dest or (dest.endswith("/") and path.startswith(dest)):
            return dest
    return None


def _absent_lines(old: Manifest, absent: list) -> list:
    lines = []
    for item in absent:
        group, dest = item if isinstance(item, tuple) else ("", item)
        hits = [(p, g) for p, (_b, g) in old.body.items() if absent_dest(p, [dest])]
        name = group or "/".join(sorted({g for _p, g in hits})) or dest
        lines.append(f"группа {name}: источник не найден — "
                     f"её {len(hits)} {_files_word(len(hits))} не судятся")
    return lines


def _percent_limit(percent) -> tuple:
    """(порог или None, причина отказа или None). Ключ приходит сырым значением: «не число»
    и «не задан» — разные причины, и обе откладывают снятие, а не разрешают его."""
    if percent is None:
        return None, REASON_NO_KEY
    try:
        limit = float(percent)
    except (TypeError, ValueError):
        return None, f"{REASON_BAD_KEY}: {percent}"
    if not math.isfinite(limit) or limit < 0:
        return None, f"{REASON_BAD_KEY}: {percent}"
    return limit, None


def _defer_reason(count: int, total: int, percent) -> tuple:
    """(причина откладывания кандидатов или None, строка порога). Порог судится только здесь."""
    limit, bad = _percent_limit(percent)
    if bad:
        return bad, ""
    share = count / total * 100 if total else 0
    if share > limit:
        return REASON_THRESHOLD, (
            f"порог: снять {count} из {total} ({share:g} %) "
            f"больше LAYOUT_REMOVE_MAX_PERCENT={limit:g} % — отложено")
    return None, ""


def judge_extra(old: Manifest, plan: set, absent: list, project: str, percent,
                lift: bool = False, lift_only=None, ours=None) -> Verdict:
    """Порядок суда одного пути: запретный, группа без источника, «уже нет»,
    состояние на диске, кандидат, порог — и порог только здесь. Довод (lift) снимает порог,
    но не предохранитель группы, и только для lift_only (None — для всех кандидатов): новый
    кандидат, которого прежний прогон не откладывал, порог по-прежнему держит.
    ours(путь) → blob диска, если правка — прежняя версия нашего же файла по истории пакета,
    иначе None: такой «правлен здесь» тоже кандидат, и снимается он с blob диска (verdict.seeded)."""
    verdict = Verdict()
    verdict.old_rows = len(old.body)
    verdict.percent = percent
    verdict.new_body = {path: (blob, group) for path, blob, group in plan}
    project_real = os.path.realpath(project)
    candidates = set()

    def edited_but_ours(path: str, reason: str | None) -> bool:
        disk = ours(path) if ours and reason == "правлен здесь" else None
        if disk:
            verdict.seeded[path] = disk
        return bool(disk)

    for path, (blob, group) in sorted(old.body.items(), key=_by_bytes):
        if path in verdict.new_body:
            continue
        tier = 1 if listable(path) else forbidden_tier(path)
        if tier == 1:
            verdict.broken.add(path)
        elif tier == 2:
            verdict.not_removable.add(path)
            verdict.new_body[path] = (blob, group)
        elif absent_dest(path, absent) is not None:
            verdict.deferred[path] = REASON_NO_SOURCE
            verdict.new_body[path] = (blob, group)
        elif not os.path.lexists(os.path.join(project, path)):
            verdict.gone.add(path)
        else:
            reason = disk_state(project, project_real, path, blob)
            if reason is None or edited_but_ours(path, reason):
                candidates.add(path)
                verdict.new_body[path] = (blob, group)
            else:
                verdict.new_kept[path] = (blob, reason)
    # «Оставлено» судится заново: правку могли откатить, файл — стать ссылкой. Кандидат
    # отсюда группы не знает, поэтому в тело не идёт: при отложенном снятии он остаётся
    # в «оставлено» с прежней причиной.
    from_kept = set()
    for path, (blob, reason) in old.kept.items():
        if path in verdict.new_body or path in verdict.new_kept:
            continue
        if listable(path) or forbidden_tier(path) == 1:
            verdict.broken.add(path)
        elif not os.path.lexists(os.path.join(project, path)):
            verdict.gone.add(path)
        else:
            now = disk_state(project, project_real, path, blob)
            verdict.new_kept[path] = (blob, reason if now is None else now)
            if now is None or edited_but_ours(path, now):
                from_kept.add(path)
    candidates |= from_kept
    verdict.absent_lines = _absent_lines(old, absent)
    total = len(old.body) + len(old.kept)
    free = set()
    if lift:
        free = candidates if lift_only is None else candidates & set(lift_only)
    rest = candidates - free
    why, line = _defer_reason(len(candidates), total, percent) if rest else (None, "")
    if rest and why:
        for path in rest:
            verdict.deferred[path] = why
        verdict.threshold_line = line
    else:
        free |= rest
    verdict.would_remove = free
    for path in free & from_kept:
        verdict.revived[path] = verdict.new_kept.pop(path)[0]
    verdict.kept = dict(verdict.new_kept)
    return verdict
