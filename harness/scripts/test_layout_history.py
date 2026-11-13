#!/usr/bin/env python3
"""Проба модуля истории раскладки (lib/layout_history.py, Ф-д): чтение истории пакета
одной командой git, перевод путей пакета в пути проекта, суд «наш по истории»,
неизвестные файлы проекта, вопрос владельцу и его согласие «да, убрать».

Модуль берётся РЯДОМ с пробой (Path(__file__).parent / "lib"): мутации подставляет
mutation-probe.sh копией корня scripts/, и мутированный модуль обязан дойти до пробы.

История — настоящая: каждый случай собирает свой стенд-пакет `git init` в своём
mktemp, blob считает `git hash-object`, а не проба по памяти. Записи согласий — в
форме, которую пишет диспетчер (`tg-dispatcher.sh` zapisat_soglasiya: ts `date -Is`,
text в нижнем регистре, message_id строкой, kind). Живой LOG_DIR, проект, каталог
копий и /etc/harness не читаются и не пишутся; сторож канала зовётся только на чтение
своих потолков.

Правила (перевод, ярусы) судятся на ТАБЛИЦЕ СТЕНДА — данные здесь лишь подставка.
Сама таблица данных (layout-history-paths.yaml) и её сверка с публичной сборкой
судятся в H3 и H4 по файлам РЯДОМ.

Модуля нет — каждый случай красный «нет модуля», а не трасса и не зелень.
Итог: «ИСТОРИЯ РАСКЛАДКИ: путей N, неудач M», код 0/1.
"""
import datetime as dt
import errno
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
MODULE_PATH = (HERE / "lib" / "layout_history.py").absolute()
TABLE_NAME = "layout-history-paths.yaml"
PUBLIC_BUILD_NAME = "public-build.yaml"
CHANNEL_GUARD = HERE / "check-channel-messages.py"

# Проба может идти из git-хука (ворота в pre-commit): GIT_DIR и соседи увели бы и
# стенд, и модуль в репозиторий проекта. Стенды судятся только своими каталогами.
for _name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX"):
    os.environ.pop(_name, None)

STAND_ENV = dict(os.environ, GIT_AUTHOR_NAME="стенд", GIT_AUTHOR_EMAIL="stand@example.invalid",
                 GIT_COMMITTER_NAME="стенд", GIT_COMMITTER_EMAIL="stand@example.invalid",
                 GIT_CONFIG_NOSYSTEM="1")

# Таблица стенда: те же формы ключей, что у данных (точный путь, префикс, null,
# вне_корней), чтобы правило судилось без опоры на файл данных.
STAND_TABLE = """\
перевод:
  "harness/hooks/project-settings.json": ".claude/settings.json"
  "harness/hooks/": "scripts/hooks/"
  "харнес/hooks/": "scripts/hooks/"
  "harness/scripts/": "scripts/"
  "харнес/scripts/": "scripts/"
  "harness/память/": null
  "harness/": "harness/"
  "UNIFIED/": "UNIFIED/"
  "sluzhebnoe/UNIFIED/": "UNIFIED/"
вне_корней:
  "харнес/": "харнес/"
"""


def load_module():
    if not MODULE_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("layout_history", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Probe:
    def __init__(self) -> None:
        self.paths = 0
        self.failures = 0

    def check(self, label: str, name: str, ok: bool, detail: str = "") -> None:
        self.paths += 1
        if ok:
            print(f"  ок    {label} {name}")
        else:
            self.failures += 1
            print(f"  ПЛОХО {label} {name}: {detail}")

    def crashed(self, label: str, error: Exception | None) -> None:
        self.paths += 1
        self.failures += 1
        if LH is None:
            print(f"  ПЛОХО {label} нет модуля {MODULE_PATH}")
        else:
            print(f"  ПЛОХО {label} проба упала: {type(error).__name__}: {error}")


PROBE = Probe()
LH = None


# ─────────────────────────────── помощники ───────────────────────────────────

def git(repo: Path, *args: str, data: bytes | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          env=STAND_ENV)
    if proc.returncode != 0:
        raise AssertionError(f"стенд: git {' '.join(args)} → {proc.returncode}: "
                             f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "surrogateescape").strip()


def new_package(root: Path) -> Path:
    """Пустой репозиторий стенда на ветке master (без `init -b`: у клиента git 2.25)."""
    repo = root / "пакет"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/master")
    return repo


def commit(repo: Path, files: dict, message: str) -> str:
    """files: путь → байты (None — удалить). Возвращает 40 hex коммита."""
    for rel, data in files.items():
        path = repo / rel
        if data is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "--allow-empty",
        "-m", message)
    return git(repo, "rev-parse", "HEAD")


def blob_of(data: bytes) -> str:
    """Настоящий blob — тем же git, что пишет историю."""
    proc = subprocess.run(["git", "hash-object", "--stdin"], input=data, capture_output=True,
                          env=STAND_ENV, check=True)
    return proc.stdout.decode().strip()


def stand_rules(root: Path):
    path = root / "таблица-стенда.yaml"
    path.write_text(STAND_TABLE, encoding="utf-8")
    return LH.load_rules(path)


def history(repo, rules):
    result = LH.read_history(str(repo), rules)
    if type(result).__name__ == "NoHistory":
        raise AssertionError(f"история не прочитана: {getattr(result, 'reason', result)!r}")
    return result


def blobs_at(hist, rel: str) -> set:
    return set(hist.blobs.get(rel) or ())


def unreadable(value) -> bool:
    """«Прочитать не смогли» — отдельный ответ с непустой причиной, не None и не запись."""
    return type(value).__name__ == "Unreadable" and bool(str(getattr(value, "reason", "")))


def no_history(value) -> str | None:
    if type(value).__name__ != "NoHistory":
        return None
    return str(getattr(value, "reason", "")) or "(пустая причина)"


def empty_answer():
    """Ответ ours для пустого blob (решение смены (д)): константа модуля или «EMPTY»."""
    return getattr(LH, "EMPTY", "EMPTY")


def is_empty_answer(value) -> bool:
    return (value is not None and not isinstance(value, bool) and value == empty_answer())


def beside_config(name: str) -> Path | None:
    """Файл данных РЯДОМ: проект (scripts/ → harness/config) или пакет (harness/scripts/
    → harness/config). Третье место — рабочий каталог: прибор мутаций копирует только
    корень scripts/ и зовёт пробу из корня проекта; таблица — данные, не механизм."""
    for folder in (HERE.parent / "harness" / "config", HERE.parent / "config",
                   Path.cwd() / "harness" / "config"):
        if (folder / name).is_file():
            return folder / name
    return None


Q = dt.datetime(2026, 10, 20, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=5)))


def iso(moment: dt.datetime) -> str:
    return moment.isoformat()


def consent_row(text: str, moment: dt.datetime | str, mid: str | None = "9400",
                kind: str | None = "any") -> str:
    """Строка confirmations.jsonl в форме диспетчера (jq -c: ts, text, message_id, kind)."""
    row = {"ts": moment if isinstance(moment, str) else iso(moment), "text": text}
    if mid is not None:
        row["message_id"] = mid
    if kind is not None:
        row["kind"] = kind
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def asked_row(moment: dt.datetime | str, paths: list) -> str:
    """Строка раскладка-спрошено.jsonl по договору: ts, пути, вопрос."""
    return json.dumps({"ts": moment if isinstance(moment, str) else iso(moment),
                       "пути": paths, "вопрос": "На сервере …"}, ensure_ascii=False)


def log_dir_with(root: Path, name: str, asked: list | None, consents: list | None) -> Path:
    folder = root / name
    folder.mkdir()
    if asked is not None:
        (folder / "раскладка-спрошено.jsonl").write_text(
            "".join(line + "\n" for line in asked), encoding="utf-8")
    if consents is not None:
        (folder / "confirmations.jsonl").write_text(
            "".join(line + "\n" for line in consents), encoding="utf-8")
    return folder


def carrier(folder: Path):
    """Носитель слова стенда: папка — каталог стенда. Судья согласия читает слово у носителя,
    не у каталога вопроса; модуль owner_word — тот, что импортировал модуль под пробой."""
    return LH.owner_word.Носитель(папка=folder, uid=None, паспорт={}, граница=folder.parent,
                                  окно_мин=60, срок_кнопки_мин=15)


def consent_for(root: Path, name: str, rows: list, asked_at=Q):
    folder = log_dir_with(root, name, [asked_row(asked_at, ["harness/ОПИСЬ.md"])], rows)
    question = LH.last_question(folder)
    if question is None or unreadable(question):
        raise AssertionError(f"вопрос стенда не прочитан: {question!r}")
    return LH.consent_after(question, carrier(folder))


def stand_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="layout-history-probe-"))


# ─────────────────────────────── случаи ──────────────────────────────────────

def case_h1() -> None:
    """История берёт ветки и теги, но не remotes; путь с кириллицей — ключ дословно; blob — 40 hex.
    Стенд: master (A, A2), ветка «страховка» с harness/ОПИСЬ.md = X, коммит только под тегом,
    коммит только под refs/remotes/x/master.
    Зелен на поломке? `--all` — нет: blob удалённой ссылки и счёт коммитов его выдают, и пара
    (ветка снята, тот же коммит под remotes) обязана потерять X; `--abbrev` — нет: X сверяется
    40-знаковым; снятый `-z` — нет: ключ с пробелом и «ё» обязан прийти дословно; снятый
    `--tags` — нет: blob под тегом обязан найтись; перевод «harness/scripts/» → «scripts/»
    не применён — нет: оба blob a.sh ждутся под scripts/a.sh."""
    root = stand_root()
    try:
        rules = stand_rules(root)
        repo = new_package(root)
        a1, a2 = b"#!/bin/sh\necho a1\n", b"#!/bin/sh\necho a2\n"
        x_data = "# опись прежней раскладки\nharness/старое.md\n".encode()
        odd_rel, odd_data = "harness/старое/список ё.md", "ёлка\n".encode()
        commit(repo, {"harness/scripts/a.sh": a1}, "A")
        commit(repo, {"harness/scripts/a.sh": a2}, "A2")
        git(repo, "checkout", "-q", "-b", "страховка")
        insurance = commit(repo, {"harness/ОПИСЬ.md": x_data, odd_rel: odd_data}, "страховка")
        git(repo, "checkout", "-q", "master")
        git(repo, "checkout", "-q", "--detach")
        tag_data = b"tagged\n"
        commit(repo, {"harness/по-тегу.md": tag_data}, "под тегом")
        git(repo, "tag", "v-проба")
        remote_data = b"remote only\n"
        remote = commit(repo, {"harness/удалённое.md": remote_data}, "только remotes")
        git(repo, "update-ref", "refs/remotes/x/master", remote)
        git(repo, "checkout", "-q", "master")

        x = blob_of(x_data)
        hist = history(repo, rules)
        PROBE.check("H1", "X по harness/ОПИСЬ.md из ветки «страховка»",
                    x in blobs_at(hist, "harness/ОПИСЬ.md"),
                    f"blobs[harness/ОПИСЬ.md] = {sorted(blobs_at(hist, 'harness/ОПИСЬ.md'))}")
        PROBE.check("H1", "все blob пути — 40 hex",
                    all(re.fullmatch(r"[0-9a-f]{40}", b) for b in blobs_at(hist, "harness/ОПИСЬ.md"))
                    and bool(blobs_at(hist, "harness/ОПИСЬ.md")),
                    f"{sorted(blobs_at(hist, 'harness/ОПИСЬ.md'))}")
        PROBE.check("H1", "harness/scripts/a.sh → scripts/a.sh, оба blob истории",
                    blobs_at(hist, "scripts/a.sh") == {blob_of(a1), blob_of(a2)},
                    f"scripts/a.sh {sorted(blobs_at(hist, 'scripts/a.sh'))}, ключи {sorted(hist.blobs)[:8]}")
        PROBE.check("H1", "путь с пробелом и «ё» — ключ дословно (-z)",
                    blobs_at(hist, odd_rel) == {blob_of(odd_data)},
                    f"ключи истории {sorted(hist.blobs)}")
        PROBE.check("H1", "blob под тегом найден (--tags)",
                    blob_of(tag_data) in blobs_at(hist, "harness/по-тегу.md"),
                    f"ключи истории {sorted(hist.blobs)}")
        PROBE.check("H1", "blob только под refs/remotes НЕ найден (не --all)",
                    "harness/удалённое.md" not in hist.blobs, f"ключи истории {sorted(hist.blobs)}")
        expect = int(git(repo, "rev-list", "--count", "--branches", "--tags"))
        PROBE.check("H1", f"commits = rev-list --branches --tags ({expect})",
                    hist.commits == expect, f"commits {hist.commits!r}")
        PROBE.check("H1", "полный клон стенда — shallow False", hist.shallow is False,
                    f"shallow {hist.shallow!r}")

        git(repo, "branch", "-D", "страховка")
        git(repo, "update-ref", "refs/remotes/x/master", insurance)
        hist = history(repo, rules)
        PROBE.check("H1", "пара: ветка снята, коммит только под remotes → X нет",
                    x not in blobs_at(hist, "harness/ОПИСЬ.md"),
                    f"blobs[harness/ОПИСЬ.md] = {sorted(blobs_at(hist, 'harness/ОПИСЬ.md'))}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h2() -> None:
    """Пустой blob — отдельный ответ EMPTY (решение смены (д)), не True и не False.
    Зелен на поломке? «снято правило пустого blob» — нет: пустой blob есть в истории пути,
    без правила ours ответил бы True; «пустой = False» — нет: ответ обязан быть EMPTY;
    «EMPTY раньше, чем путь известен» — нет: путь без истории обязан дать None."""
    root = stand_root()
    try:
        rules = stand_rules(root)
        repo = new_package(root)
        commit(repo, {"harness/ПУСТО.md": b"", "harness/ЕЩЁ.md": b"not empty\n"}, "A")
        hist = history(repo, rules)
        empty = blob_of(b"")
        PROBE.check("H2", "EMPTY_BLOB модуля = git hash-object пустого файла",
                    getattr(LH, "EMPTY_BLOB", None) == empty, f"EMPTY_BLOB {getattr(LH, 'EMPTY_BLOB', None)!r}")
        PROBE.check("H2", "история знает пустой blob по пути (стенд верен)",
                    empty in blobs_at(hist, "harness/ПУСТО.md"), f"{sorted(blobs_at(hist, 'harness/ПУСТО.md'))}")
        answer = LH.ours(hist, "harness/ПУСТО.md", empty)
        PROBE.check("H2", "пустой файл, пустой blob в истории → EMPTY", is_empty_answer(answer),
                    f"ours = {answer!r}")
        answer = LH.ours(hist, "harness/ЕЩЁ.md", empty)
        PROBE.check("H2", "пустой файл, путь известен без пустого blob → EMPTY",
                    is_empty_answer(answer), f"ours = {answer!r}")
        answer = LH.ours(hist, "harness/НЕТ.md", empty)
        PROBE.check("H2", "пустой файл, путь истории не известен → None", answer is None,
                    f"ours = {answer!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h3() -> None:
    """Перевод вперёд: самый длинный совпавший префикс по ОБЕИМ таблицам, null — «раскладка
    туда не клала», ключ без «/» — точный путь. Сначала таблица стенда в обратном порядке
    (короткие ключи первыми), затем таблица данных рядом и история стенда.
    Зелен на поломке? «первый совпавший» — нет: на таблице стенда короткий ключ стоит
    первым; «null не судится» — нет: путь под null-ключом обязан дать None, а не перевод
    более короткого; «точный ключ как префикс» — нет: «a/b/c.txtx» идёт по «a/b/»;
    «вне_корней судится отдельно и раньше» — нет: «x/in/» в переводе длиннее «x/» вне корней."""
    root = stand_root()
    try:
        reverse = root / "обратная.yaml"
        reverse.write_text(
            "перевод:\n"
            '  "a/": "p/"\n'
            '  "a/b/": "q/"\n'
            '  "a/b/c.txt": "r.txt"\n'
            '  "a/n/": null\n'
            '  "x/in/": "in/"\n'
            '  "y/": "y/"\n'
            "вне_корней:\n"
            '  "x/": "x/"\n'
            '  "y/out/": "yo/"\n', encoding="utf-8")
        rules = LH.load_rules(reverse)
        for pkg, want in (("a/b/c.txt", "r.txt"), ("a/b/d.txt", "q/d.txt"), ("a/e.txt", "p/e.txt"),
                          ("a/n/f.txt", None), ("a/b/c.txtx", "q/c.txtx"), ("x/in/f.txt", "in/f.txt"),
                          ("zzz/f.txt", None)):
            got = LH.project_path(pkg, rules)
            PROBE.check("H3", f"стенд: {pkg} → {want}", got == want, f"получено {got!r}")
        got = LH.project_path("y/out/f.txt", rules)
        PROBE.check("H3", "стенд: y/out/f.txt — вне корней, не перевод короткого «y/»",
                    got != "y/out/f.txt", f"получено {got!r}")

        table = beside_config(TABLE_NAME)
        PROBE.check("H3", f"таблица данных {TABLE_NAME} рядом найдена", table is not None,
                    f"нет ни в {HERE.parent / 'harness' / 'config'}, ни в {HERE.parent / 'config'}")
        if table is not None:
            data_rules = LH.load_rules(table)
            for pkg, want in (
                    ("harness/hooks/a.py", "scripts/hooks/a.py"),
                    ("harness/hooks/project-settings.json", ".claude/settings.json"),
                    ("harness/scripts/lib/x.py", "scripts/lib/x.py"),
                    ("харнес/scripts/y.py", "scripts/y.py"),
                    ("харнес/hooks/x.py", "scripts/hooks/x.py"),
                    ("harness/ОПИСЬ.md", "harness/ОПИСЬ.md"),
                    ("harness/память/z.md", None),
                    ("harness/STATE.md", None),
                    ("harness/STATE.md.orig", "harness/STATE.md.orig"),
                    ("harness/dev-map.yaml", None),
                    ("harness/razlozhit-harnes.sh", None),
                    ("harness/mechanisms.yaml", None),
                    ("harness/config/logrotate-harness", None),
                    ("harness/config/models.yaml", "harness/config/models.yaml"),
                    ("sluzhebnoe/UNIFIED/01.md", "UNIFIED/01.md"),
                    ("UNIFIED/02.md", "UNIFIED/02.md"),
                    ("harness/UNIFIED/03.md", "UNIFIED/03.md"),
                    ("README.md", None)):
                got = LH.project_path(pkg, data_rules)
                PROBE.check("H3", f"данные: {pkg} → {want}", got == want, f"получено {got!r}")

        rules = stand_rules(root)
        repo = new_package(root)
        commit(repo, {"харнес/ОПИСЬ.md": b"old outside\n", "харнес/hooks/x.py": b"hook\n",
                      "harness/память/z.md": b"memory\n", "sluzhebnoe/UNIFIED/01.md": b"chapter\n"},
               "старая раскладка")
        hist = history(repo, rules)
        PROBE.check("H3", "история: харнес/ОПИСЬ.md — в outside",
                    blob_of(b"old outside\n") in set(hist.outside.get("харнес/ОПИСЬ.md") or ()),
                    f"outside {dict((k, sorted(v)) for k, v in hist.outside.items())}")
        PROBE.check("H3", "история: харнес/ОПИСЬ.md — не в blobs",
                    not any(k.startswith("харнес/") for k in hist.blobs), f"blobs {sorted(hist.blobs)}")
        PROBE.check("H3", "история: харнес/hooks/x.py → blobs[scripts/hooks/x.py], не outside",
                    blob_of(b"hook\n") in blobs_at(hist, "scripts/hooks/x.py")
                    and "харнес/hooks/x.py" not in hist.outside,
                    f"blobs {sorted(hist.blobs)}, outside {sorted(hist.outside)}")
        PROBE.check("H3", "история: harness/память/ не переведён никуда",
                    not any("память" in k for k in list(hist.blobs) + list(hist.outside)),
                    f"blobs {sorted(hist.blobs)}")
        PROBE.check("H3", "история: sluzhebnoe/UNIFIED/01.md → UNIFIED/01.md",
                    blob_of(b"chapter\n") in blobs_at(hist, "UNIFIED/01.md"), f"blobs {sorted(hist.blobs)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h4() -> None:
    """Таблица данных против публичной сборки: каждый перенос `перенести` (источник →
    место в сборке) переводится таблицей так же, как его источник. Множество переносов
    непустое — пустое было бы зелёным без работы.
    Зелен на поломке? «строки sluzhebnoe/UNIFIED/ нет» — нет: перенос UNIFIED обязан дать
    тот же путь проекта; «файла сборки нет» — нет: не найден = ПРОВАЛ."""
    import yaml
    build = beside_config(PUBLIC_BUILD_NAME)
    table = beside_config(TABLE_NAME)
    PROBE.check("H4", f"{PUBLIC_BUILD_NAME} и {TABLE_NAME} рядом найдены",
                build is not None and table is not None, f"сборка {build}, таблица {table}")
    if build is None or table is None:
        return
    moves = (yaml.safe_load(build.read_text(encoding="utf-8")) or {}).get("перенести") or {}
    PROBE.check("H4", "переносов в сборке > 0", len(moves) > 0, f"перенести = {moves!r}")
    rules = LH.load_rules(table)
    for source, target in moves.items():
        name = "проба-h4/глава.md"
        want = LH.project_path(f"{str(source).rstrip('/')}/{name}", rules)
        got = LH.project_path(f"{str(target).rstrip('/')}/{name}", rules)
        PROBE.check("H4", f"перенос {source} → {target}: путь проекта как у источника ({want})",
                    want is not None and got == want, f"источник → {want!r}, сборка → {got!r}")


def case_h5() -> None:
    """ours: путь без истории → None; правка в 1 байт → False; blob другого пути → False;
    blob из истории пути → True.
    Зелен на поломке? «путь известен — значит наш» — нет: правка в 1 байт обязана дать
    False; «blob где угодно в истории» — нет: blob a.sh по пути ОПИСЬ.md — False."""
    root = stand_root()
    try:
        rules = stand_rules(root)
        repo = new_package(root)
        x_data, a_data = "# опись\n".encode(), b"echo a\n"
        commit(repo, {"harness/ОПИСЬ.md": x_data, "harness/scripts/a.sh": a_data}, "A")
        hist = history(repo, rules)
        x = blob_of(x_data)
        for name, rel, blob, want in (
                ("путь без истории → None", "harness/НЕТ.md", x, None),
                ("правка в 1 байт → False", "harness/ОПИСЬ.md", blob_of(x_data + b"!"), False),
                ("blob другого пути → False", "harness/ОПИСЬ.md", blob_of(a_data), False),
                ("blob из истории пути → True", "harness/ОПИСЬ.md", x, True)):
            got = LH.ours(hist, rel, blob)
            PROBE.check("H5", name, got is want, f"ours = {got!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h6() -> None:
    """unknown_files: только корни укладки, без яруса 1/2, без известного, без назначений
    группы без источника (строкой и парой), без __pycache__, ссылки на каталог не проходятся.
    Зелен на поломке? «followlinks» — нет: файл за ссылкой обязан не появиться; «ярус не
    судится» — нет: models.yaml, .установлено, .layout-tmp, skills; «absent только
    строками» — нет: второй зов подаёт пары."""
    root = stand_root()
    try:
        project, outside = root / "проект", root / "вне"
        files = ("harness/ОПИСЬ.md", "harness/config/models.yaml",
                 "harness/vozmozhnosti/a/.установлено", "scripts/.x.layout-tmp",
                 "harness/skills/s/SKILL.md", "UNIFIED/g.md", "scripts/__pycache__/m.pyc",
                 "scripts/a.sh", "docs/чужое.md", "STATE.md")
        for rel in files:
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            (project / rel).write_bytes(b"x\n")
        outside.mkdir()
        (outside / "за-ссылкой.md").write_bytes(b"x\n")
        os.symlink(outside, project / "scripts" / "ln")
        for absent in (["UNIFIED/"], [("unified", "UNIFIED/")]):
            got = LH.unknown_files(project, {"scripts/a.sh"}, absent)
            PROBE.check("H6", f"absent {absent!r} → ровно [harness/ОПИСЬ.md]",
                        sorted(got) == ["harness/ОПИСЬ.md"], f"получено {sorted(got)}")
        PROBE.check("H6", "имя судьи назначений публичное: layout_manifest.absent_dest",
                    callable(getattr(LH.layout_manifest, "absent_dest", None))
                    and not hasattr(LH.layout_manifest, "_absent_dest"), "нет absent_dest или цел _absent_dest")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h7() -> None:
    """seed_verdict: would_remove — только обычный файл с родителем внутри проекта и blob
    истории; правленый — foreign; без истории — strangers; пустой — empty; файл проекта под
    вне_корней с blob истории — outside.
    Зелен на поломке? «ссылка-файл тоже снимается» — нет: ЛИНК.md с тем же blob X; «родитель
    не судится» — нет: harness/вне/ — ссылка на каталог вне проекта с файлом X; «outside
    без сверки blob» — нет: харнес/ДРУГОЕ.md правлен."""
    root = stand_root()
    try:
        rules = stand_rules(root)
        repo = new_package(root)
        x_data, p_data, o_data, d_data = (b"# opis X\n", b"edited later\n", b"outside old\n",
                                          b"outside other\n")
        commit(repo, {"harness/ОПИСЬ.md": x_data, "harness/ЛИНК.md": x_data,
                      "harness/вне/ОПИСЬ.md": x_data, "harness/ПРАВКА.md": p_data,
                      "harness/ПУСТО.md": b"", "харнес/ОПИСЬ.md": o_data,
                      "харнес/ДРУГОЕ.md": d_data}, "старое")
        hist = history(repo, rules)
        project = root / "проект"
        ext_file, ext_dir = root / "чужое" / "x.md", root / "чужой-каталог"
        ext_file.parent.mkdir(parents=True)
        ext_file.write_bytes(x_data)
        ext_dir.mkdir()
        (ext_dir / "ОПИСЬ.md").write_bytes(x_data)
        layout = {"harness/ОПИСЬ.md": x_data, "harness/ПРАВКА.md": p_data + b"!",
                  "harness/ЧУЖОЕ.md": b"never in history\n", "scripts/чужое.sh": b"echo\n",
                  "harness/ПУСТО.md": b"", "харнес/ОПИСЬ.md": o_data,
                  "харнес/ДРУГОЕ.md": d_data + b"!"}
        for rel, data in layout.items():
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            (project / rel).write_bytes(data)
        os.symlink(ext_file, project / "harness" / "ЛИНК.md")
        os.symlink(ext_dir, project / "harness" / "вне")
        unknown = ["harness/ОПИСЬ.md", "harness/ЛИНК.md", "harness/вне/ОПИСЬ.md",
                   "harness/ПРАВКА.md", "harness/ЧУЖОЕ.md", "scripts/чужое.sh", "harness/ПУСТО.md"]
        seed = LH.seed_verdict(project, unknown, hist)
        PROBE.check("H7", "would_remove = {harness/ОПИСЬ.md: X} (ссылки — нет)",
                    dict(seed.would_remove) == {"harness/ОПИСЬ.md": blob_of(x_data)},
                    f"would_remove {dict(seed.would_remove)}")
        PROBE.check("H7", "foreign = [harness/ПРАВКА.md]", sorted(seed.foreign) == ["harness/ПРАВКА.md"],
                    f"foreign {seed.foreign!r}")
        PROBE.check("H7", "strangers = 2", seed.strangers == 2, f"strangers {seed.strangers!r}")
        PROBE.check("H7", "empty = 1", getattr(seed, "empty", None) == 1,
                    f"empty {getattr(seed, 'empty', None)!r}")
        PROBE.check("H7", "outside = 1 (харнес/ОПИСЬ.md, не правленый ДРУГОЕ)", seed.outside == 1,
                    f"outside {seed.outside!r}")
        # FD-04: путь, которого история не знает, не читается. Нечитаемый чужой файл
        # (права 000) считается чужим по lstat; чтение уронило бы суд PermissionError.
        locked = project / "harness" / "ЗАКРЫТО.md"
        locked.write_bytes(b"closed\n")
        locked.chmod(0)
        try:
            if os.access(locked, os.R_OK):
                PROBE.check("H7", "стенд: файл с правами 000 нечитаем для пробы", False,
                            "проба идёт от root — случай не воспроизвести")
            else:
                try:
                    seed = LH.seed_verdict(project, ["harness/ЗАКРЫТО.md"], hist)
                    PROBE.check("H7", "неизвестный нечитаемый файл: strangers 1, без чтения",
                                seed.strangers == 1, f"strangers {seed.strangers!r}")
                except OSError as trouble:
                    PROBE.check("H7", "неизвестный нечитаемый файл: strangers 1, без чтения", False,
                                f"суд читал файл, которого история не знает: {trouble}")
        finally:
            locked.chmod(0o644)
        # Один хешер: поток равен git hash-object и на файле крупнее куска чтения.
        big = project / "harness" / "БОЛЬШОЙ.bin"
        big.write_bytes(os.urandom(2 * 1024 * 1024 + 17))
        want = subprocess.run(["git", "hash-object", str(big)], capture_output=True, text=True,
                              env=STAND_ENV, check=True).stdout.strip()
        PROBE.check("H7", "layout_manifest.git_blob = git hash-object на файле 2 МиБ",
                    LH.layout_manifest.git_blob(str(big)) == want, f"хеш {want!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h8() -> None:
    """Согласие: запись диспетчера с message_id, слова «да» И «убрать» с границей по буквам,
    момент ПОСЛЕ вопроса (сравнение моментов, не строк), не позже 7 суток после вопроса (граница ≤).
    Зелен на поломке? «"да" in text» — нет: «когда убрать?»; граница только слева — нет:
    «дабы убрать»; «без message_id годится» — нет; «ts ≥ вопроса» — нет: ts = вопросу;
    «сравнение строк ts» — нет: две пары с разными поясами расходятся с порядком строк;
    «< 7 сут» — нет: ровно 7 суток — согласие; «окна нет» — нет: 7 сут + 1 с; «окно от сейчас» — нет: ответ дня 1 обязан быть годным."""
    root = stand_root()
    try:
        after = Q + dt.timedelta(seconds=60)
        got = consent_for(root, "годное", [consent_row("да", Q - dt.timedelta(hours=1), "9399"),
                                           consent_row("да, убрать", after, "9400")])
        PROBE.check("H8", "«да, убрать» после вопроса с message_id → запись",
                    isinstance(got, dict) and got.get("message_id") == "9400"
                    and "убрать" in str(got.get("text")), f"получено {got!r}")
        for number, (name, rows) in enumerate((
                ("«да» any после вопроса → None", [consent_row("да", after)]),
                ("«убрать» без «да» → None", [consent_row("убрать", after, kind=None)]),
                ("«когда убрать?» → None", [consent_row("когда убрать?", after, kind=None)]),
                ("«дабы убрать» → None", [consent_row("дабы убрать", after, kind=None)]),
                ("«да, убрать» без message_id → None", [consent_row("да, убрать", after, mid=None)]),
                ("«да, убрать» за 60 с ДО вопроса → None",
                 [consent_row("да, убрать", Q - dt.timedelta(seconds=60))]),
                ("«да, убрать» в ту же секунду, что вопрос → None", [consent_row("да, убрать", Q)]),
                ("«да» и «убрать» двумя записями (две строки сообщения) → None",
                 [consent_row("да", after), consent_row("убрать", after, kind=None)]))):
            got = consent_for(root, f"случай-{number}", rows)
            PROBE.check("H8", name, got is None, f"получено {got!r}")

        # Окно считается от ВОПРОСА до ответа, не от «сейчас»: раскладка идёт со следующим
        # обновлением, и согласие дня 1 обязано дожить до раскладки дня 10 (FD-02).
        edge = Q + dt.timedelta(days=7)
        got = consent_for(root, "ответ-через-день", [consent_row("да, убрать", Q + dt.timedelta(days=1))])
        PROBE.check("H8", "ответ через день → запись, даже если раскладка только через 10 суток",
                    isinstance(got, dict), f"получено {got!r}")
        got = consent_for(root, "ровно-7", [consent_row("да, убрать", edge)])
        PROBE.check("H8", "ответ ровно через 7 суток после вопроса → запись (≤)",
                    isinstance(got, dict), f"получено {got!r}")
        got = consent_for(root, "7-и-1с", [consent_row("да, убрать", edge + dt.timedelta(seconds=1))])
        PROBE.check("H8", "ответ через 7 суток + 1 с после вопроса → None", got is None, f"получено {got!r}")
        got = consent_for(root, "8-суток", [consent_row("да, убрать", Q + dt.timedelta(days=8))])
        PROBE.check("H8", "согласие старше 7 суток после вопроса не годится → None",
                    got is None, f"получено {got!r}")

        utc = dt.timezone.utc
        got = consent_for(root, "пояс-после", [consent_row("да, убрать", "2026-10-20T05:30:00+00:00")])
        PROBE.check("H8", "пояса: 05:30+00:00 позже 10:00+05:00 (строкой — раньше) → запись",
                    isinstance(got, dict), f"получено {got!r}")
        got = consent_for(root, "пояс-до", [consent_row("да, убрать", "2026-10-20T09:30:00+05:00")],
                          asked_at=dt.datetime(2026, 10, 20, 5, 0, tzinfo=utc))
        PROBE.check("H8", "пояса: 09:30+05:00 раньше 05:00+00:00 (строкой — позже) → None",
                    got is None, f"получено {got!r}")
        got = consent_for(root, "без-пояса", [consent_row("да, убрать", "2026-10-20T10:01:00")])
        PROBE.check("H8", "ts без пояса → Unreadable (не TypeError)", unreadable(got), f"получено {got!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h9() -> None:
    """Нечитаемое ≠ «нет»: битая строка согласий → Unreadable, даже рядом с годным согласием;
    файла согласий нет → None; битый журнал вопроса → Unreadable; последний вопрос — по
    наибольшему ts, не по порядку строк; record_question → last_question возвращает то же.
    Зелен на поломке? «битая строка пропускается» — нет: рядом годное «да, убрать», ответ
    записью был бы ложным; «битая = пусто» — нет: обязан быть Unreadable, не None;
    «последняя строка» — нет: поздний ts стоит первой строкой."""
    root = stand_root()
    try:
        folder = log_dir_with(root, "битые-согласия", [asked_row(Q, ["harness/ОПИСЬ.md"])],
                              [consent_row("да, убрать", Q + dt.timedelta(minutes=1)), "{не json"])
        got = LH.consent_after(LH.last_question(folder), carrier(folder))
        PROBE.check("H9", "битая строка согласий → Unreadable", unreadable(got), f"получено {got!r}")
        folder = log_dir_with(root, "нет-согласий", [asked_row(Q, ["harness/ОПИСЬ.md"])], None)
        got = LH.consent_after(LH.last_question(folder), carrier(folder))
        PROBE.check("H9", "файла согласий нет → None", got is None, f"получено {got!r}")

        # Слово — у носителя, а не рядом с вопросом: годное «да, убрать» в каталоге вопроса,
        # носитель — другой каталог без согласий → None. Носитель не прочитан → Unreadable.
        folder = log_dir_with(root, "согласие-у-вопроса", [asked_row(Q, ["harness/ОПИСЬ.md"])],
                              [consent_row("да, убрать", Q + dt.timedelta(minutes=1))])
        other = log_dir_with(root, "носитель-пуст", None, [])
        got = LH.consent_after(LH.last_question(folder), carrier(other))
        PROBE.check("H9", "согласие в каталоге вопроса, носитель другой → None (слово у носителя)",
                    got is None, f"получено {got!r}")
        got = LH.consent_after(LH.last_question(folder), carrier(folder))
        PROBE.check("H9", "пара: тот же каталог носителем → запись", isinstance(got, dict),
                    f"получено {got!r}")
        machine = LH.owner_word.носитель
        def no_carrier():
            raise LH.owner_word.НетНосителя("паспорт стенда не прочитан")
        LH.owner_word.носитель = no_carrier
        try:
            got = LH.consent_after(LH.last_question(folder))
        finally:
            LH.owner_word.носитель = machine
        PROBE.check("H9", "носитель не дан и машинный не прочитан → Unreadable, не None",
                    unreadable(got) and "паспорт стенда не прочитан" in str(got.reason),
                    f"получено {got!r}")

        folder = log_dir_with(root, "битый-вопрос", [asked_row(Q, ["a"]), "мусор"], None)
        got = LH.last_question(folder)
        PROBE.check("H9", "битая строка журнала вопроса → Unreadable", unreadable(got), f"получено {got!r}")
        folder = log_dir_with(root, "вопрос-без-пояса", [asked_row("2026-10-20T10:00:00", ["a"])], None)
        got = LH.last_question(folder)
        PROBE.check("H9", "ts вопроса без пояса → Unreadable", unreadable(got), f"получено {got!r}")
        folder = log_dir_with(root, "нет-вопроса", None, None)
        got = LH.last_question(folder)
        PROBE.check("H9", "журнала вопроса нет → None", got is None, f"получено {got!r}")
        late = Q + dt.timedelta(days=2)
        folder = log_dir_with(root, "порядок", [asked_row(late, ["harness/поздний.md"]),
                                                 asked_row(Q, ["harness/ранний.md"])], None)
        got = LH.last_question(folder)
        PROBE.check("H9", "последний вопрос — по наибольшему ts, не по строке",
                    got is not None and not unreadable(got) and got.ts == late
                    and list(got.paths) == ["harness/поздний.md"], f"получено {got!r}")

        folder = log_dir_with(root, "запись", None, None)
        LH.record_question(folder, Q, ["harness/ОПИСЬ.md"], "текст вопроса")
        lines = (folder / "раскладка-спрошено.jsonl").read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0]) if len(lines) == 1 else {}
        PROBE.check("H9", "record_question: одна строка {ts, пути, вопрос}",
                    set(row) == {"ts", "пути", "вопрос"} and row.get("пути") == ["harness/ОПИСЬ.md"]
                    and row.get("вопрос") == "текст вопроса", f"строки {lines!r}")
        got = LH.last_question(folder)
        PROBE.check("H9", "record_question → last_question: тот же момент и пути",
                    got is not None and not unreadable(got) and got.ts == Q
                    and list(got.paths) == ["harness/ОПИСЬ.md"], f"получено {got!r}")

        # FD-07: журнал LOG_DIR читается с пределом. Ссылка на /dev/zero не бесконечна для
        # пробы: предел обрывает чтение, ответ — Unreadable, не память и не «согласия нет».
        folder = log_dir_with(root, "бесконечный", [asked_row(Q, ["harness/ОПИСЬ.md"])], None)
        os.symlink("/dev/zero", folder / "confirmations.jsonl")
        got = LH.consent_after(LH.last_question(folder), carrier(folder))
        PROBE.check("H9", "согласия — ссылка на /dev/zero → Unreadable (чтение с пределом)",
                    unreadable(got), f"получено {got!r}")

        # Итог прогона для `update.py --затравка`: запись и чтение того же, «нет файла» ≠ «битый».
        folder = log_dir_with(root, "итог", None, None)
        got = LH.last_seed_view(folder)
        PROBE.check("H9", "итога прогона нет → None", got is None, f"получено {got!r}")
        LH.record_seed_view(folder, Q, ["harness/a, b.md", "harness/ОПИСЬ.md"], "строка двери")
        got = LH.last_seed_view(folder)
        PROBE.check("H9", "record_seed_view → last_seed_view: тот же момент, пути (с «, » в имени), строка",
                    got is not None and not unreadable(got) and got.ts == Q
                    and list(got.paths) == ["harness/a, b.md", "harness/ОПИСЬ.md"]
                    and got.line == "строка двери", f"получено {got!r}")
        (folder / "раскладка-затравка.json").write_text("{не json", encoding="utf-8")
        got = LH.last_seed_view(folder)
        PROBE.check("H9", "битый итог прогона → Unreadable", unreadable(got), f"получено {got!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def channel_guard(text: str) -> tuple[int, str]:
    if not CHANNEL_GUARD.is_file():
        return -1, f"нет сторожа канала {CHANNEL_GUARD}"
    proc = subprocess.run([sys.executable, str(CHANNEL_GUARD), "--текст", text],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, (proc.stdout + proc.stderr).strip()[:300]


def case_h10() -> None:
    """question_text: текст §5 с «да, убрать» и числом НАЗВАННЫХ путей; длинный список режется
    под потолок, asked — ровно названные пути; сторож канала пропускает оба текста.
    Зелен на поломке? «текст называет пути вне asked» — нет: ни один путь вне asked не
    должен встретиться; «asked = все пути» — нет: 60 путей не влезают в 700 знаков."""
    ceiling = 700
    text, asked = LH.question_text(["harness/ОПИСЬ.md"], ceiling)
    PROBE.check("H10", "1 путь: asked = [harness/ОПИСЬ.md]", list(asked) == ["harness/ОПИСЬ.md"],
                f"asked {asked!r}")
    PROBE.check("H10", "1 путь: «На сервере 1 файл …», путь, «Ответь «да, убрать».»",
                "На сервере 1 файл от прежних версий харнеса: harness/ОПИСЬ.md." in text
                and "Убрать в архив? Ответь ровно «да, убрать»." in text, f"текст {text!r}")
    rc, said = channel_guard(text)
    PROBE.check("H10", "1 путь: сторож канала пропускает (rc 0)", rc == 0, f"rc {rc}: {said}")
    # FD-13: слово, которое текст просит ответить, судья диспетчера признаёт согласием.
    asked_word = re.search(r"Ответь ровно «([^»]+)»", text)
    judged = subprocess.run([sys.executable, str(HERE / "lib" / "patterns.py"), "--вердикт-согласия",
                             asked_word.group(1) if asked_word else ""],
                            capture_output=True, text=True, timeout=60)
    # «да, убрать» — ответ на названное действие: судья отдаёт вид «адресовано».
    PROBE.check("H10", "ответ из текста вопроса судья диспетчера признаёт (адресовано)",
                asked_word is not None and judged.stdout.strip() == "адресовано",
                f"слово {asked_word and asked_word.group(1)!r}, судья: {judged.stdout.strip()!r} {judged.stderr.strip()[:100]!r}")
    for n, word in ((3, "файла"), (5, "файлов")):
        text, asked = LH.question_text([f"harness/ст{i}.md" for i in range(n)], ceiling)
        PROBE.check("H10", f"{n} путей → «{n} {word}»", f"На сервере {n} {word} " in text,
                    f"текст {text!r}")

    paths = [f"harness/старое/файл-{i:02d}.md" for i in range(60)]
    text, asked = LH.question_text(paths, ceiling)
    asked = list(asked)
    PROBE.check("H10", "60 путей: 0 < asked < 60", 0 < len(asked) < 60, f"asked {len(asked)}")
    PROBE.check("H10", f"60 путей: текст ≤ {ceiling} знаков", len(text) <= ceiling, f"{len(text)} знаков")
    PROBE.check("H10", "60 путей: asked ⊆ путей", set(asked) <= set(paths), f"asked {asked[:5]}")
    PROBE.check("H10", "60 путей: каждый путь asked назван в тексте",
                all(p in text for p in asked), f"не названы {[p for p in asked if p not in text][:5]}")
    extra = [p for p in paths if p not in asked and p in text]
    PROBE.check("H10", "60 путей: ни один путь вне asked не назван", not extra, f"названы {extra[:5]}")
    PROBE.check("H10", "60 путей: число в тексте = len(asked)",
                re.search(rf"На сервере {len(asked)} файл", text) is not None, f"текст {text[:80]!r}")
    rc, said = channel_guard(text)
    PROBE.check("H10", "60 путей: сторож канала пропускает (rc 0)", rc == 0, f"rc {rc}: {said}")


NO_GIT_PROGRAM = r"""
import errno, importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("layout_history", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = module.read_history(sys.argv[3], module.load_rules(sys.argv[2]))
print(json.dumps({"kind": type(result).__name__, "reason": str(getattr(result, "reason", "")),
                  "strerror": os.strerror(errno.ENOENT)}, ensure_ascii=False))
"""


def case_h11() -> None:
    """NoHistory с причиной: каталог без .git; «-» → «пакет без истории»; git нет в PATH →
    слова ОС; git отказал (объект коммита утрачен) → первая строка stderr; shallow-клон →
    shallow True, полный → False.
    Зелен на поломке? «shallow всегда True» — нет: полный стенд обязан дать False; «отказ git
    = пустая история» — нет: ответ обязан быть NoHistory с первой строкой stderr. Отказ по
    владельцу каталога (safe.directory) без root не воспроизвести — его заменяет утрата
    объекта: та же ветка «git ответил не нулём»."""
    root = stand_root()
    try:
        rules = stand_rules(root)
        plain = root / "без-git"
        plain.mkdir()
        precondition = subprocess.run(["git", "-C", str(plain), "rev-parse", "--git-dir"],
                                      capture_output=True, env=STAND_ENV)
        PROBE.check("H11", "стенд: каталог без .git не внутри чужого репозитория",
                    precondition.returncode != 0, f"git нашёл {precondition.stdout!r}")
        reason = no_history(LH.read_history(str(plain), rules))
        PROBE.check("H11", "каталог без .git → NoHistory с причиной", bool(reason), f"причина {reason!r}")
        reason = no_history(LH.read_history("-", rules))
        PROBE.check("H11", "«-» → «пакет без истории»", reason is not None and "пакет без истории" in reason,
                    f"причина {reason!r}")

        repo = new_package(root)
        for i in range(3):
            commit(repo, {"harness/scripts/a.sh": f"echo {i}\n".encode()}, f"c{i}")
        table = root / "таблица-стенда.yaml"
        proc = subprocess.run([sys.executable, "-c", NO_GIT_PROGRAM, str(MODULE_PATH), str(table), str(repo)],
                              capture_output=True, text=True, timeout=60,
                              env=dict(STAND_ENV, PATH="/nonexistent"))
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {"kind": "?", "reason": (proc.stdout + proc.stderr).strip()[-300:], "strerror": "?"}
        PROBE.check("H11", "git нет в PATH → NoHistory со словами ОС",
                    out["kind"] == "NoHistory" and out["strerror"] in out["reason"],
                    f"{out!r}")

        full = history(repo, rules)
        PROBE.check("H11", "полный стенд → shallow False", full.shallow is False, f"shallow {full.shallow!r}")
        clone = root / "клон"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(clone)],
                       capture_output=True, env=STAND_ENV, check=True)
        shallow = history(clone, rules)
        PROBE.check("H11", "shallow-клон → shallow True", shallow.shallow is True,
                    f"shallow {shallow.shallow!r}")

        first = git(repo, "rev-list", "--max-parents=0", "HEAD")
        (repo / ".git" / "objects" / first[:2] / first[2:]).unlink()
        broken = subprocess.run(["git", "-C", str(repo), *LH.LOG_ARGS], capture_output=True, env=STAND_ENV)
        stderr_first = broken.stderr.decode("utf-8", "replace").strip().splitlines()
        PROBE.check("H11", "стенд: git отказал на утраченном объекте", broken.returncode != 0 and bool(stderr_first),
                    f"rc {broken.returncode}")
        reason = no_history(LH.read_history(str(repo), rules))
        PROBE.check("H11", "git отказал → NoHistory с первой строкой stderr",
                    reason is not None and bool(stderr_first) and stderr_first[0] in reason,
                    f"причина {reason!r}, stderr {stderr_first[:1]!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h12() -> None:
    """Стоимость: 50 коммитов по 20 путей — read_history зовёт git не больше двух раз
    (log и rev-parse) и разбирает всё (страж от git на путь/коммит и от квадратичного
    разбора судится числом зовов, не секундомером: под нагрузкой ворот время плавает).
    Зелен на поломке? «git на каждый путь» — нет: 1000 зовов; «git на каждый коммит» — нет: 50."""
    root = stand_root()
    try:
        rules = stand_rules(root)
        repo = new_package(root)
        for i in range(50):
            commit(repo, {f"harness/scripts/f{j:02d}.sh": f"echo {i} {j}\n".encode() for j in range(20)},
                   f"c{i}")
        real_git = shutil.which("git")
        shim_dir = root / "ложный-git"
        shim_dir.mkdir()
        counter = root / "зовы-git.log"
        shim = shim_dir / "git"
        shim.write_text(f'#!/bin/bash\necho call >> "{counter}"\nexec "{real_git}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        saved = os.environ["PATH"]
        os.environ["PATH"] = f"{shim_dir}:{saved}"
        try:
            hist = history(repo, rules)
        finally:
            os.environ["PATH"] = saved
        calls = len(counter.read_text().splitlines()) if counter.is_file() else 0
        PROBE.check("H12", f"read_history на 50 коммитах: зовов git 1..2 (было {calls})", 1 <= calls <= 2,
                    f"зовов {calls}")
        PROBE.check("H12", "50 коммитов, 20 путей × 50 blob",
                    hist.commits == 50 and len(hist.blobs) == 20
                    and all(len(v) == 50 for v in hist.blobs.values()),
                    f"commits {hist.commits}, путей {len(hist.blobs)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ──────────── мусор на входе (разведка фаззингом, hr.fuzzing-for-special-tasks) ────────────
# Входы — дословно из пробы /var/log/harness/fuzz-probe.log. Исключение, вылетевшее из
# модуля, судится как ответ «упало: <тип>», а не роняет весь случай: у каждого дефекта
# своя красная строка.

FUZZ_TS = "2026-10-20T10:00:00+00:00"


def attempt(call):
    """(ответ, None) или (None, «Тип: текст») — исключение модуля наружу."""
    try:
        return call(), None
    except Exception as error:          # the module must answer by value, any escape is red
        return None, f"{type(error).__name__}: {str(error)[:80]}"


def question_paths(value) -> list | None:
    if type(value).__name__ != "Question":
        return None
    return list(value.paths)


def case_h13() -> None:
    """Разделитель строк внутри пути не рвёт журнал вопроса: record_question → last_question
    возвращает те же пути для U+2028, U+2029 и NEL (\\x85) — json.dumps(ensure_ascii=False)
    пишет их сырыми, а splitlines() считает концом строки. Пара: путь без них.
    Зелен на поломке? «экранирован один U+2028» — нет: в списке три символа, каждый своим
    путём; «битая строка пропускается» — нет: пути обязаны вернуться дословно, а не None."""
    root = stand_root()
    try:
        folder = log_dir_with(root, "здоровый", None, None)
        LH.record_question(folder, Q, ["scripts/ab.py"], "текст вопроса")
        got = LH.last_question(folder)
        PROBE.check("H13", "пара: путь без разделителей → тот же путь",
                    question_paths(got) == ["scripts/ab.py"], f"получено {got!r}")
        paths = ["scripts/a b.py", "scripts/a b.py", "scripts/a\x85b.py"]
        folder = log_dir_with(root, "разделители", None, None)
        LH.record_question(folder, Q, paths, "текст вопроса")
        got, error = attempt(lambda: LH.last_question(folder))
        PROBE.check("H13", "U+2028, U+2029, NEL в путях: record_question → last_question те же пути",
                    error is None and question_paths(got) == paths,
                    f"получено {error or repr(got)}")
        lines = (folder / "раскладка-спрошено.jsonl").read_bytes().split(b"\n")
        PROBE.check("H13", "запись вопроса — одна строка JSON при разбиении по '\\n'",
                    len(lines) == 2 and lines[1] == b"" and attempt(lambda: json.loads(lines[0]))[1] is None,
                    f"строк {len(lines)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h14() -> None:
    """«пути» не список → Unreadable, а не TypeError наружу и не список букв.
    Пара: «пути» списком → Question с тем же списком.
    Зелен на поломке? «поймать TypeError» — нет: строка проходит list() и даёт буквы, её
    случай отдельный; «пустой список вместо мусора» — нет: обязан быть Unreadable."""
    root = stand_root()
    try:
        folder = log_dir_with(root, "список", [json.dumps({"ts": FUZZ_TS, "пути": ["scripts/a.py"]})], None)
        got = LH.last_question(folder)
        PROBE.check("H14", "пара: «пути» списком → тот же список",
                    question_paths(got) == ["scripts/a.py"], f"получено {got!r}")
        for name, value in (("число", 5), ("строка", "scripts/a.py")):
            folder = log_dir_with(root, name, [json.dumps({"ts": FUZZ_TS, "пути": value})], None)
            got, error = attempt(lambda: LH.last_question(folder))
            PROBE.check("H14", f"«пути»: {value!r} → Unreadable", error is None and unreadable(got),
                        f"получено {error or repr(got)}")
        # Список, но не строк: проверка «список» его пропустит, судит только «элементы — строки».
        # Зелен на поломке? «судим только тип списка» — нет: тогда вернётся Question с [5, None];
        # «Unreadable без причины» — нет: причина обязана назвать «не список строк».
        for name, value in (("числа", [5, 7]), ("смесь", ["scripts/a.py", None]),
                            ("вложенный", [["scripts/a.py"]])):
            folder = log_dir_with(root, name, [json.dumps({"ts": FUZZ_TS, "пути": value})], None)
            got, error = attempt(lambda: LH.last_question(folder))
            PROBE.check("H14", f"«пути»: список не-строк {value!r} → Unreadable с причиной «не список строк»",
                        error is None and unreadable(got) and "не список строк" in got.reason,
                        f"получено {error or repr(got)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_h15() -> None:
    """Глубокая вложенность JSON (`[`×100000) → Unreadable, не RecursionError: и журнал вопроса,
    и итог прогона. Пара: неглубокий список путей читается.
    Зелен на поломке? «ловить в _rows» — нет: last_seed_view судится отдельно; «строка
    пропускается» — нет: обязан быть Unreadable, а не None."""
    root = stand_root()
    try:
        folder = log_dir_with(root, "мелкий", [json.dumps({"ts": FUZZ_TS, "пути": [["a"]]})], None)
        got, error = attempt(lambda: LH.last_question(folder))
        PROBE.check("H15", "пара: вложенность 2 → ответ значением", error is None and got is not None,
                    f"получено {error or repr(got)}")
        for name, line in (("голый", "[" * 100000),
                           ("в-путях", '{"ts": "%s", "пути": %s' % (FUZZ_TS, "[" * 100000))):
            folder = log_dir_with(root, name, [line], None)
            got, error = attempt(lambda: LH.last_question(folder))
            PROBE.check("H15", f"журнал вопроса: `[`×100000 ({name}) → Unreadable",
                        error is None and unreadable(got), f"получено {error or repr(got)}")
        folder = log_dir_with(root, "итог", None, None)
        (folder / "раскладка-затравка.json").write_text("[" * 100000, encoding="utf-8")
        got, error = attempt(lambda: LH.last_seed_view(folder))
        PROBE.check("H15", "итог прогона: `[`×100000 → Unreadable", error is None and unreadable(got),
                    f"получено {error or repr(got)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def git_shim(folder: Path, log_bytes: bytes) -> Path:
    """Ложный git: `log` отдаёт заданные байты, `rev-parse` — «false». Настоящий git байт
    не из UTF-8 в поле blob не выдаст — вход чужой, его форму диктует другая сторона."""
    folder.mkdir()
    (folder / "вывод.bin").write_bytes(log_bytes)
    shim = folder / "git"
    shim.write_text('#!/bin/bash\nfor a in "$@"; do\n  [ "$a" = log ] && exec cat "%s"\n'
                    '  [ "$a" = rev-parse ] && { echo false; exit 0; }\ndone\nexit 2\n'
                    % (folder / "вывод.bin"), encoding="utf-8")
    shim.chmod(0o755)
    return folder


def history_through(shim_dir: Path, rules):
    saved = os.environ["PATH"]
    os.environ["PATH"] = f"{shim_dir}:{saved}"
    try:
        return attempt(lambda: LH.read_history(str(shim_dir), rules))
    finally:
        os.environ["PATH"] = saved


def case_h16() -> None:
    """Байт не из UTF-8 в поле blob вывода git → read_history отвечает значением, не
    UnicodeDecodeError: годная пара того же коммита в истории, битая — нет (или NoHistory с
    причиной). Пара: тот же вывод без битой пары → blob на месте.
    Зелен на поломке? «весь разбор в try → пустая History» — нет: годная пара обязана дойти;
    «битый blob как строка с заменой» — нет: «\\ufffd…» в blobs красит."""
    root = stand_root()
    try:
        rules = stand_rules(root)
        good_blob = "b" * 40
        good = b":100644 100644 " + b"0" * 40 + b" " + good_blob.encode() + b" M\0harness/scripts/b.py\0"
        bad = b":100644 100644 " + b"a" * 40 + b" \xff\xfe M\0harness/scripts/a.py\0"
        head = b"\x01" + b"c" * 40 + b"\n\0"
        hist, error = history_through(git_shim(root / "здоровый-git", head + good), rules)
        PROBE.check("H16", "пара: годный вывод → blob scripts/b.py на месте",
                    error is None and type(hist).__name__ == "History"
                    and blobs_at(hist, "scripts/b.py") == {good_blob}, f"получено {error or repr(hist)}")
        hist, error = history_through(git_shim(root / "битый-git", head + bad + good), rules)
        kind = type(hist).__name__
        PROBE.check("H16", "байт \\xff в поле blob → значение, не исключение; годная пара на месте",
                    error is None and (no_history(hist) is not None or (
                        kind == "History" and blobs_at(hist, "scripts/b.py") == {good_blob}
                        and not blobs_at(hist, "scripts/a.py"))),
                    f"получено {error or repr(hist)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


CASES = (case_h1, case_h2, case_h3, case_h4, case_h5, case_h6, case_h7, case_h8, case_h9,
         case_h10, case_h11, case_h12, case_h13, case_h14, case_h15, case_h16)


def main() -> int:
    global LH
    try:
        LH = load_module()
    except Exception as error:      # a broken module must show as red per case, not a trace
        print(f"модуль {MODULE_PATH} не загрузился: {type(error).__name__}: {error}")
        LH = None
    print(f"модуль: {MODULE_PATH}" + ("" if LH is not None else " — НЕТ, каждый случай красный"))
    for case in CASES:
        label = case.__name__.removeprefix("case_").upper()
        if LH is None:
            PROBE.crashed(label, None)
            continue
        try:
            case()
        except Exception as error:      # a probe bug must show as red, not as a trace
            PROBE.crashed(label, error)
    print(f"ИСТОРИЯ РАСКЛАДКИ: путей {PROBE.paths}, неудач {PROBE.failures}")
    return 0 if PROBE.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
