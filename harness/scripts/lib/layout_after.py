#!/usr/bin/env python3
"""Журнал отката знает и то, что пишут шаги мимо двери укладки.

Дверь (layout-door.py) пишет в journal.tsv только файлы своего плана. Остальное
обновление пишет мимо неё: слияние живых настроек (pipeline.yaml) — до двери,
импорты в CLAUDE.md, карточки помощников, производные документы — после.
Находка копии bkb 14.11.2026: откат по журналу вернул 225 путей, а дубль строки
CLAUDE.md, новый ключ pipeline.yaml и новая карточка harness-explorer.md остались.

Поэтому разница «до раскладки → сейчас» по всему дереву проекта дописывается в
тот же журнал теми же строками, что пишет дверь: откат читает журнал любой
версией двери (формат строк тот же), и старый откатчик копии вернёт и их.

  layout_after.py snap <проект> <снимок.tsv>
      снимок до раскладки: путь, blob, режим каждого файла дерева git (кроме
      игнорируемых); байты кладутся в объекты git проекта (hash-object -w) —
      из них потом берётся копия в архив.
  layout_after.py record <проект> <архив> [--snapshot <снимок.tsv>]
      дописать в <архив>/journal.tsv пути, чьё состояние на диске не то, что
      оставил журнал (или снимок, если пути в журнале нет). Снимок кладётся в
      архив (до-раскладки.tsv): второй вызов (update.py после своих шагов)
      берёт его оттуда и дописывает только новое.

Проект не репозиторий git — снимка нет, сказано словами, код 0: журнал двери
остаётся как был.
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout_journal       # noqa: E402
import layout_manifest      # noqa: E402

SNAPSHOT_NAME = "до-раскладки.tsv"
NOTE_PREFIX = "# шаг после двери: "


def _git(project: str, *args, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", project, *args], input=data, capture_output=True)


def is_repo_root(project: str) -> bool:
    top = _git(project, "rev-parse", "--show-toplevel")
    return top.returncode == 0 and os.path.realpath(top.stdout.decode().strip()) == os.path.realpath(project)


def tree_paths(project: str) -> list:
    """Файлы дерева, как их видит git status: отслеживаемые и неигнорируемые новые."""
    listed = _git(project, "-c", "core.quotepath=off", "ls-files", "-z", "--cached",
                  "--others", "--exclude-standard")
    if listed.returncode != 0:
        raise OSError(f"git ls-files: {listed.stderr.decode(errors='replace').strip()}")
    names = {name for name in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if name}
    # Перевод строки в имени hash-object --stdin-paths не разберёт.
    return sorted(name for name in names if "\n" not in name)


def regular(project: str, rel: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(os.path.join(project, rel)).st_mode)
    except OSError:
        return False


def disk_mode(project: str, rel: str) -> int:
    return stat.S_IMODE(os.lstat(os.path.join(project, rel)).st_mode)


def snap(project: str, out: str) -> int:
    if not is_repo_root(project):
        print(f"шаги после двери в журнал не пишутся: {project} не корень репозитория git")
        return 0
    files = [rel for rel in tree_paths(project) if regular(project, rel)]
    # Один нечитаемый файл (журнал демона, заведённый root) валил hash-object всего списка,
    # и откат терял все шаги после двери (Content-Factory 15.11.2026, 0.12.2). Его
    # раскладка не пишет: в снимок не идёт, но назван.
    paths = [rel for rel in files if os.access(os.path.join(project, rel), os.R_OK)]
    unreadable = sorted(set(files) - set(paths))
    hashed = _git(project, "hash-object", "-w", "--stdin-paths",
                  data="".join(f"{rel}\n" for rel in paths).encode("utf-8", "surrogateescape"))
    if hashed.returncode != 0:
        raise OSError(f"git hash-object: {hashed.stderr.decode(errors='replace').strip()}")
    blobs = hashed.stdout.decode().split()
    with open(out, "w", encoding="utf-8", errors="surrogateescape") as handle:
        for rel, blob in zip(paths, blobs, strict=True):
            mode = disk_mode(project, rel)
            handle.write(f"{rel}\t{blob}\t{mode:o}\n")
    print(f"снимок до раскладки: файлов {len(paths)}")
    if unreadable:
        shown = ", ".join(unreadable[:10]) + (" …" if len(unreadable) > 10 else "")
        print(f"вне снимка, нечитаемы: {len(unreadable)}: {shown}")
    return 0


def read_snapshot(path: str) -> dict:
    found = {}
    with open(path, encoding="utf-8", errors="surrogateescape") as handle:
        for line in handle:
            rel, blob, mode = line.rstrip("\n").split("\t")
            found[rel] = (blob, int(mode, 8))
    return found


def journal_state(journal) -> dict:
    """Путь → blob, который журнал оставил на диске (None — путь снят)."""
    state = {}
    for action in journal.actions:
        state[action.path] = None if action.now == "-" else action.now
    return state


def copy_from_git(project: str, blob: str, mode: int, final: str) -> None:
    shown = _git(project, "cat-file", "blob", blob)
    if shown.returncode != 0:
        raise OSError(f"git cat-file {blob}: {shown.stderr.decode(errors='replace').strip()}")
    os.makedirs(os.path.dirname(final), exist_ok=True)
    temp = os.path.join(os.path.dirname(final), f".{os.path.basename(final)}.copy-tmp")
    with open(temp, "wb") as handle:
        handle.write(shown.stdout)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, final)


def open_journal(archive: str):
    """Дескриптор журнала на дозапись; нет журнала — новый с шапкой (дверь архива не открывала)."""
    path = os.path.join(archive, "journal.tsv")
    created = not os.path.lexists(path)
    os.makedirs(archive, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if created:
        write_line(fd, f"# прогон {os.getpid()} обновление план=0 перекрыть=0")
    return fd, created


def write_line(fd: int, text: str) -> None:
    data = (text + "\n").encode("utf-8", "surrogateescape")
    if os.write(fd, data) != len(data):
        raise OSError("журнал не записан: запись короче строки")
    os.fsync(fd)


def drift(project: str, before: dict, left: dict) -> list:
    """[(действие, было, стало, путь)] путей, чей диск не тот, что ждёт журнал или снимок.

    «Было» — всегда состояние ДО раскладки (снимок): откат возвращает к нему, а
    его байты есть и в объектах git, и в копии двери, если путь трогала она."""
    project_real = os.path.realpath(project)
    changes = []
    for rel in sorted(set(before) | set(left) | set(tree_paths(project))):
        disk = layout_manifest.project_blob(project, project_real, rel)
        if disk == layout_journal.NOT_A_FILE:
            continue        # ссылку и каталог журнал не выражает
        own, own_mode = before.get(rel, (None, 0))
        if disk == own and rel not in left and own is not None and disk_mode(project, rel) != own_mode:
            # Тот же blob, другой режим (Content-Factory 15.11.2026: test_deploy_ci.sh
            # остался 100755 после отката): копия в архиве несёт режим снимка.
            changes.append(("перекрыт", own, own, rel))
            continue
        if disk in (own, left.get(rel, own)):
            continue        # уже «до» — откатывать нечего; или ровно то, что знает журнал
        if own is None:
            changes.append(("положен-новый", "-", disk, rel))
        elif disk is None:
            changes.append(("снят", own, "-", rel))
        else:
            changes.append(("перекрыт", own, disk, rel))
    return changes


def record(project: str, archive: str, snapshot: str | None) -> int:
    kept = os.path.join(archive, SNAPSHOT_NAME)
    if snapshot and os.path.isfile(snapshot) and not os.path.exists(kept):
        os.makedirs(archive, exist_ok=True)
        shutil.copyfile(snapshot, kept)
    if not os.path.isfile(kept):
        print(f"шаги после двери не записаны: снимка до раскладки нет ({kept})")
        return 0
    before = read_snapshot(kept)
    journal, reason = layout_journal.read_journal(archive)
    if reason:
        print(f"шаги после двери не записаны: журнал не прочитан: {reason}", file=sys.stderr)
        return 1
    left = journal_state(journal) if journal is not None else {}
    changes = drift(project, before, left)
    if not changes:
        print("шаги после двери: изменений вне журнала нет")
        return 0
    files = os.path.join(archive, "files")
    fd, created = open_journal(archive)
    written = 0
    try:
        for kind, was, now, rel in changes:
            if kind != "положен-новый":
                final = os.path.join(files, rel)
                held = layout_manifest.git_blob(final) if os.path.isfile(final) else None
                if held not in (None, was):
                    write_line(fd, f"{NOTE_PREFIX}{rel}: копия в архиве не та — путь не записан")
                    continue
                if held is None:
                    copy_from_git(project, was, before[rel][1], final)
            write_line(fd, f"{kind}\t{was}\t{now}\t{rel}")
            write_line(fd, f"готово\t-\t-\t{rel}")
            written += 1
        if created:
            write_line(fd, f"итог\t-\t-\t{written}")
    finally:
        os.close(fd)
    print(f"шаги после двери записаны в журнал отката: путей {written}")
    return 0


def main(argv: list) -> int:
    if len(argv) == 3 and argv[0] == "snap":
        return snap(argv[1], argv[2])
    if len(argv) in (3, 5) and argv[0] == "record" and (len(argv) == 3 or argv[3] == "--snapshot"):
        return record(argv[1], argv[2], argv[4] if len(argv) == 5 else None)
    print(__doc__.strip().split("\n\n")[2], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
