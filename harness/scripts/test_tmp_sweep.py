#!/usr/bin/env python3
"""Пробы уборки /tmp (scripts/tmp-sweep.py) — пишутся ДО кода.

Механизм УДАЛЯЕТ файлы (И-1): каждая защита — свой случай со своей причиной.
Случаи — контракт `.tasks/pb.raise-disk-autocleanup-efficiency/pretask.yaml`
(`sick_cases`, `probe_cases.tmp_sweep`), план — Task 7.

Договор, который судят пробы (уточняет интерфейс контракта там, где он молчит):
* модуль рядом с пробой (`tmp-sweep.py`), данные — `../config/tmp-sweep.yaml`
  (пакет) или `../harness/config/tmp-sweep.yaml` (проект);
* `sweep(root, mode, *, expected_uid, holders, geteuid, lstat, mountinfo,
  after_rename, journal, cfg, now, dry_run)`; `mode` — «ночь» | «давление»;
  `holders` — ВЫЗЫВАЕМОЕ без аргументов → Holders | NoRight: держатели читаются
  до отбора и ЗАНОВО после rename (повторная проверка видит нового держателя);
* `lstat` зовётся для КАЖДОГО узла записи — и при сборе фактов, и при удалении —
  как `lstat(имя, dir_fd=fd)` или `lstat(путь)`; проба узнаёт узел по последнему
  компоненту имени и подменяет `st_dev`/`st_uid` (без root иначе не создать);
* `after_rename` зовётся после переноса в карантин и до повторной проверки;
* журнал удалений — JSON-строки через `\\n`: `когда, путь (прежний полный путь
  записи), мб, возраст_ч, правило, класс, действие`; действие — «удалено» |
  «в карантин <хеш12>» | «возвращено» | «отказ: <причина>» | строка со словом
  «осталось» при частичном удалении; первая строка отказа записи — её причина;
* `Report.decisions` — {имя записи: Decision(.action ∈ удалить|карантин|оставить|отказ)};
* `read_holders()` зовёт `sudo -n` ПО ИМЕНИ из PATH; `--держатели` печатает JSON;
* `--selftest-full` пишет в файл `TMP_SWEEP_SELFTEST_NOTE` (если задан) первой
  строкой путь своего стенда, второй — «удалено N»;
* `--сверить-журнал` судит «путь внутри /tmp», не «прямой ребёнок /tmp».

Договор круга 1 (решения смены по ревью `review-code-round1.md`):
* C-1: строка «в карантин <хеш12>» (с `путь` и `куда`) ложится в журнал ДО
  rename записи в карантин; не легла — rename нет. Запись карантина, хеш
  которой не назван ни одной строкой журнала, не удаляется НИКОГДА; итог хода
  печатает «без строки журнала: N». Исчезнувший узел — не исключение хода.
* M-1: `--ночь`/`--давление` без `--dry-run` сам скрипт пускает только при
  `TMP_SWEEP_ENABLED=1` (harness.conf по `HARNESS_CONF`, окружение старше
  файла, пустое = не задано, нет ключа/файла = 0); иначе код 2 и причина со
  словом `TMP_SWEEP_ENABLED` ДО лока и до держателей. `--dry-run`,
  `--вернуть`, `--сверить-журнал`, `--selftest-full` выключатель не судит.
* M-2: «след-», «класс-», «больной-», «входящие-» — не отходы (обычные слова).
* M-3: запись глубже предела обхода — «отказ: слишком глубоко», ход идёт дальше.
* M-4: класс «неизвестное» не удаляется и не переносится ни в каком режиме и
  ни через какой срок; строка карантина с классом «неизвестное» тоже не
  удаляется по сроку. Карантин и сроки — только для «отходов».
* m1: держатель — и исполняемый файл процесса (`exe`), и отображённый в память.
* m3: `restore` при отказе rename отвечает словами, не трассой.
* m5: недоудалённое («осталось узлов») не считается в `Report.deleted`.

Безопасность самой пробы: боевой `/tmp` не участвует — каждый ход идёт по
`/tmp/уборка-стенд-*/корень` (сокетам нужен путь ≤ 108 байт, scratchpad длиннее);
боевой CLI зовётся только с подменённым корнем (отказ) и вдобавок `--dry-run` и
`sudo`-заглушкой: сломанная проверка корня всё равно не удалит ничего.
Прежние стенды старше 20 минут убираются по префиксу ДО работы, свои — после
каждого случая (SIGKILL прошлого прогона не оставляет мусор навсегда).

Прогон (только фоном): python3 scripts/test_tmp_sweep.py [ИМЯ_СЛУЧАЯ ...]
Итог: «УБОРКА TMP: случаев N, неудач M», код 0/1.
"""
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MECHANISM = HERE / "tmp-sweep.py"
STAND_PREFIX = "уборка-стенд-"
HOME_PREFIX = "уборка-стенд-дом-"
WASTE = "обновление-стенд-"
UNKNOWN = "неизвестное-"
QUARANTINE = ".уборка-карантин"
HOUR = 3600
DAY = 86400
# Сдвиг `now`: ctime подделать нельзя, поэтому «старое» — это `now` стенда
# на неделю вперёд; неделя больше любого срока данных (72 ч).
OLD = 7 * DAY
STALE_STAND_S = 20 * 60
NO_DIR_FOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

CREATED: list[Path] = []
PROCS: list[subprocess.Popen] = []


class Missing(Exception):
    """Нет модуля или данных — причина красного до кода."""


# ─────────────────────────────── механизм и данные ───────────────────────────

_module = None


def mod():
    global _module
    if _module is None:
        if not MECHANISM.is_file():
            raise Missing(f"нет модуля {MECHANISM.name} рядом с пробой")
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        spec = importlib.util.spec_from_file_location("tmp_sweep_under_probe", MECHANISM)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _module = module
    return _module


def config_path() -> Path | None:
    for candidate in (HERE.parent / "config" / "tmp-sweep.yaml",
                      HERE.parent / "harness" / "config" / "tmp-sweep.yaml"):
        if candidate.is_file():
            return candidate
    return None


def load_cfg() -> dict:
    path = config_path()
    if path is None:
        raise Missing("нет tmp-sweep.yaml (../config или ../harness/config)")
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def cfg_with_terms(terms: dict) -> dict:
    cfg = copy.deepcopy(load_cfg())
    cfg["сроки_ч"].update(terms)
    return cfg


# ─────────────────────────────── стенд ───────────────────────────────────────

def new_stand() -> Path:
    # dir="/tmp" явно: TMPDIR сессии указывает в scratchpad, где bind() падает по длине
    path = Path(tempfile.mkdtemp(prefix=STAND_PREFIX, dir="/tmp"))
    CREATED.append(path)
    return path


def stand_root() -> tuple[Path, Path, Path]:
    """(стенд, корень хода, журнал) — журнал вне корня, чтобы не быть записью."""
    stand = new_stand()
    root = stand / "корень"
    root.mkdir()
    return stand, root, stand / "журнал.jsonl"


def _empty_or_subdir(dir_fd: int) -> str | None:
    """Удалить не-каталоги уровня; вернуть имя подкаталога (с правом записи) или None."""
    for name in os.listdir(dir_fd):
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            os.unlink(name, dir_fd=dir_fd)
            continue
        if st.st_uid == os.getuid():
            os.chmod(name, 0o700, dir_fd=dir_fd)
        return name
    return None


def force_remove(path: Path) -> None:
    """Снести дерево стенда (каталоги 0555 случая T19, цепочка M-3 в 1200 уровней).

    Открыт один fd в каждый момент, подъём через «..»: и рекурсия, и
    shutil.rmtree держат fd на уровень и падают на глубине выше ulimit -n.
    """
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    os.chmod(path, 0o700)
    fd = os.open(path, NO_DIR_FOLLOW)
    names: list[str] = []
    try:
        while True:
            child = _empty_or_subdir(fd)
            if child is not None:
                nxt = os.open(child, NO_DIR_FOLLOW, dir_fd=fd)
                names.append(child)
            elif names:
                nxt = os.open("..", NO_DIR_FOLLOW, dir_fd=fd)
            else:
                break
            os.close(fd)
            fd = nxt
            if child is None:
                os.rmdir(names.pop(), dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(path)


def clean_stale_stands() -> int:
    """Стенды прежних прогонов, убитых SIGKILL: только свои, только старше 20 мин."""
    removed = 0
    for base, prefix in (("/tmp", STAND_PREFIX), (str(Path.home()), HOME_PREFIX)):
        for name in os.listdir(base):
            if not name.startswith(prefix):
                continue
            path = Path(base) / name
            st = os.lstat(path)
            if (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
                    and time.time() - st.st_mtime > STALE_STAND_S):
                force_remove(path)
                removed += 1
    return removed


def tree_hash(path) -> tuple[str, int]:
    """sha256 по (путь от записи, тип, режим, содержимое | цель ссылки) и число узлов.

    Обход fd-шный: глубокое дерево длиннее PATH_MAX путём не прочитать.
    """
    path = os.fspath(path)
    if not os.path.lexists(path):
        return ("нет", 0)
    digest = hashlib.sha256()
    count = 0
    parent, name = os.path.split(path)

    def visit(dir_fd: int, node: str, rel: str) -> None:
        nonlocal count
        st = os.stat(node, dir_fd=dir_fd, follow_symlinks=False)
        count += 1
        mode = st.st_mode
        digest.update(f"{rel}\0{stat.S_IFMT(mode)}\0{stat.S_IMODE(mode)}\0".encode(
            "utf-8", "surrogateescape"))
        if stat.S_ISLNK(mode):
            digest.update(os.fsencode(os.readlink(node, dir_fd=dir_fd)))
        elif stat.S_ISREG(mode):
            fd = os.open(node, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                while chunk := os.read(fd, 1 << 20):
                    digest.update(chunk)
            finally:
                os.close(fd)
        elif stat.S_ISDIR(mode):
            fd = os.open(node, NO_DIR_FOLLOW, dir_fd=dir_fd)
            try:
                for child in sorted(os.listdir(fd)):
                    visit(fd, child, f"{rel}/{child}")
            finally:
                os.close(fd)

    parent_fd = os.open(parent or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        visit(parent_fd, name, "")
    finally:
        os.close(parent_fd)
    return (digest.hexdigest(), count)


def make_tree(entry: Path, files: int = 2, size: int = 64) -> Path:
    entry.mkdir()
    (entry / "вложенный").mkdir()
    for i in range(files):
        (entry / f"файл-{i}.bin").write_bytes(os.urandom(size))
        (entry / "вложенный" / f"глубже-{i}.txt").write_text(f"строка {i}\n", encoding="utf-8")
    return entry


def backdate(path: Path, seconds: float) -> None:
    """mtime всего дерева в прошлое (ctime при этом становится «сейчас»)."""
    when = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in filenames + dirnames:
            os.utime(os.path.join(dirpath, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


# ─────────────────────────────── журнал ──────────────────────────────────────

def journal_rows(path) -> list[dict]:
    """Строки делятся ТОЛЬКО по \\n: U+2028 в имени не вправе резать запись."""
    if not os.path.exists(path):
        return []
    rows = []
    for raw in Path(path).read_bytes().split(b"\n"):
        if raw.strip():
            rows.append(json.loads(raw.decode("utf-8")))
    return rows


def actions_for(rows: list[dict], name: str) -> list[str]:
    return [str(r.get("действие", "")) for r in rows
            if os.path.basename(str(r.get("путь", ""))) == name]


def expect_refusal(rows: list[dict], name: str, reason: str) -> None:
    acts = actions_for(rows, name)
    refusals = [a for a in acts if a.startswith("отказ: ")]
    assert refusals, f"{name}: ждал «отказ: {reason}», строк записи в журнале: {acts}"
    assert refusals[0].startswith(f"отказ: {reason}"), (
        f"{name}: первая причина «{refusals[0]}», ждал «отказ: {reason}»")
    done = [a for a in acts if a == "удалено" or a.startswith("в карантин")]
    assert not done, f"{name}: при отказе ещё и {done}"


def expect_untouched(journal: Path, name: str) -> None:
    """Молодая запись не переносится вовсе: «возвращено» после rename значит, что
    её спас только второй носитель (повторная проверка), а первый сломан."""
    acts = actions_for(journal_rows(journal), name)
    assert not acts, f"{name}: молодую запись трогали: {acts}"


def names_under(path: Path) -> list[str]:
    """Имена всех узлов дерева fd-обходом: rglob глубже PATH_MAX молча слепнет."""
    found = []

    def visit(dir_fd: int) -> None:
        for child in os.listdir(dir_fd):
            found.append(child)
            if stat.S_ISDIR(os.stat(child, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                fd = os.open(child, NO_DIR_FOLLOW, dir_fd=dir_fd)
                try:
                    visit(fd)
                finally:
                    os.close(fd)

    fd = os.open(path, NO_DIR_FOLLOW)
    try:
        visit(fd)
    finally:
        os.close(fd)
    return found


def expect_intact(path: Path, before: tuple[str, int]) -> None:
    after = tree_hash(path)
    assert after == before, (
        f"{path.name}: дерево изменилось (узлов {before[1]} → {after[1]})")


# ─────────────────────────────── ход ─────────────────────────────────────────

def run_sweep(root: Path, mode: str = "ночь", *, journal: Path, now: float | None = None,
              cfg: dict | None = None, holders=None, expected_uid: int | None = None, **kw):
    """Ход по корню стенда в процессе пробы; возвращает (Report, stdout хода)."""
    assert str(root).startswith(f"/tmp/{STAND_PREFIX}"), f"ход вне стенда: {root}"
    module = mod()
    cfg = cfg if cfg is not None else load_cfg()
    print(f"  ход: {root} ({mode})", flush=True)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        report = module.sweep(
            str(root), mode,
            expected_uid=os.getuid() if expected_uid is None else expected_uid,
            holders=holders if holders is not None else module.read_holders,
            journal=str(journal), cfg=cfg,
            now=time.time() + OLD if now is None else now, **kw)
    return report, out.getvalue()


class StatProxy:
    """Ответ lstat с подменёнными полями: другая ФС и чужой uid без root."""

    def __init__(self, real, **override):
        self._real = real
        self._override = override

    def __getattr__(self, key):
        if key in self._override:
            return self._override[key]
        return getattr(self._real, key)


def lstat_substituting(node_name: str, active=lambda: True, **override):
    def fake(path, *args, **kw):
        real = os.lstat(path, *args, **kw)
        if active() and os.path.basename(os.fsdecode(path)) == node_name:
            return StatProxy(real, **override)
        return real
    return fake


# ─────────────────────────────── процессы ────────────────────────────────────

def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMP_SWEEP_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update({k: str(v) for k, v in extra.items()})
    return env


def spawn(args, **kw) -> subprocess.Popen:
    kw.setdefault("cwd", "/")
    kw.setdefault("env", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, **kw)
    PROCS.append(proc)
    return proc


def wait_word(proc: subprocess.Popen, word: str = "ok", timeout: float = 15) -> None:
    """Готовность — слово самого держателя, не сон и не возврат Popen."""
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    assert ready, f"держатель не сказал «{word}» за {timeout} с"
    line = proc.stdout.readline().strip()
    assert line == word, f"держатель сказал «{line}»"


def kill_procs() -> None:
    while PROCS:
        proc = PROCS.pop()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def fake_sudo(stand: Path) -> tuple[Path, Path]:
    """Каталог с `sudo`, который отказывает (код 1) и оставляет метку вызова."""
    bin_dir = stand / "заглушки"
    bin_dir.mkdir()
    mark = stand / "sudo-звали"
    script = bin_dir / "sudo"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> '{mark}'\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    return bin_dir, mark


def run_cli(*args, env: dict, cwd="/", timeout: float = 60, stdout=subprocess.PIPE):
    return subprocess.run([sys.executable, "-B", str(MECHANISM), *args], cwd=cwd, env=env,
                          stdout=stdout, stderr=subprocess.PIPE, text=True,
                          timeout=timeout)


LISTENER = textwrap.dedent("""\
    import os, socket, sys, time
    path = sys.stdin.readline().rstrip("\\n")
    relative = sys.argv[1] == "rel"
    s = socket.socket(socket.AF_UNIX)
    if relative:
        os.chdir(os.path.dirname(path))
        s.bind(os.path.basename(path))
        os.chdir("/")
    else:
        s.bind(path)
    s.listen(1)
    print("ok", flush=True)
    time.sleep(600)
""")

FD_HOLDER = textwrap.dedent("""\
    import sys, time
    f = open(sys.stdin.readline().rstrip("\\n"), "rb")
    print("ok", flush=True)
    time.sleep(600)
""")


def socket_listener(path: Path, relative: bool) -> None:
    proc = spawn([sys.executable, "-c", LISTENER, "rel" if relative else "abs"])
    proc.stdin.write(f"{path}\n")
    proc.stdin.flush()
    wait_word(proc)


# ─────────────────────────────── случаи ──────────────────────────────────────

def case_t1_twenty_dead_waste():
    """§7.1: ровно 20 мёртвых отходов удалены, 20 строк «удалено», имён в stdout нет."""
    _, root, journal = stand_root()
    names = [f"{WASTE}{i:02d}" for i in range(20)]
    for name in names:
        (root / name).mkdir()
        (root / name / "данные.bin").write_bytes(os.urandom(1 << 20))
    _, out = run_sweep(root, journal=journal)
    rows = journal_rows(journal)
    deleted = [r for r in rows if r.get("действие") == "удалено"]
    assert len(deleted) == 20, f"строк «удалено» {len(deleted)}, ждал 20"
    assert {os.path.basename(r["путь"]) for r in deleted} == set(names), "удалено не то"
    left = [n for n in names if os.path.lexists(root / n)]
    assert not left, f"остались на месте: {len(left)}"
    leftovers = [p for p in root.rglob("данные.bin")]
    assert not leftovers, f"файлы отходов живут где-то в корне: {len(leftovers)}"
    for row in deleted:
        assert 0.9 <= float(row["мб"]) <= 1.2, f"мб {row['мб']} у записи в 1 МиБ"
    leaked = [n for n in names if n in out]
    assert not leaked, f"имена записей в stdout хода: {len(leaked)}"


def case_t2_cli_root_substitution():
    """§7.2: TMP_SWEEP_ROOT без --selftest — код 2, «подмена корня вне стенда»,
    ни обращения к держателям, ни журнала, стенд и копия в $HOME целы."""
    mod()
    stand = new_stand()
    logs = stand / "лог"
    logs.mkdir()
    bin_dir, mark = fake_sudo(stand)
    target = stand / "цель"
    target.mkdir()
    make_tree(target / f"{WASTE}x", files=1)
    link = stand / "ссылка"
    link.symlink_to(target)
    open_dir = stand / "каталог755"
    open_dir.mkdir()
    make_tree(open_dir / f"{WASTE}y", files=1)
    open_dir.chmod(0o755)
    (stand / "rel").mkdir()
    home_copy = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir=Path.home()))
    CREATED.append(home_copy)
    make_tree(home_copy / f"{WASTE}z", files=1)
    before_stand, before_home = tree_hash(stand), tree_hash(home_copy)
    path_env = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    for value in ("", ".", "rel", "/tmp", str(home_copy), str(link), str(open_dir)):
        env = clean_env(LOG_DIR=logs, PATH=path_env, TMP_SWEEP_ROOT=value)
        result = run_cli("--ночь", "--dry-run", env=env, cwd=stand)
        said = result.stdout + result.stderr
        assert result.returncode == 2, f"TMP_SWEEP_ROOT={value!r}: код {result.returncode}"
        assert "подмена корня вне стенда" in said, f"TMP_SWEEP_ROOT={value!r}: нет причины"
    assert not mark.exists(), "держателей спросили — ход пошёл дальше отказа"
    journals = list(logs.rglob("уборка-tmp.jsonl"))
    assert not journals, "журнал удалений создан при отказе корня"
    expect_intact(stand, before_stand)
    expect_intact(home_copy, before_home)


def case_t2e_check_root_form():
    """§4.3 п.1: форма корня; симлинк на сам /tmp проходит realpath, но не форму."""
    module = mod()
    assert module.check_root("/tmp", stand=False) is None, "боевой /tmp отвергнут"
    stand = new_stand()
    to_tmp = stand / "на-tmp"
    to_tmp.symlink_to("/tmp")
    to_stand = stand / "на-стенд"
    sub = stand / "подстенд"
    sub.mkdir()
    to_stand.symlink_to(sub)
    home_copy = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir=Path.home()))
    CREATED.append(home_copy)
    for path in (str(to_tmp), str(stand), "", ".", "rel", str(home_copy)):
        reason = module.check_root(path, stand=False)
        assert isinstance(reason, str) and reason, f"боевой корень {path!r} принят"
    assert module.check_root(str(sub), stand=True) is None, "стенд из mkdtemp отвергнут"
    for path in (str(to_stand), str(home_copy), "", ".", "rel"):
        reason = module.check_root(path, stand=True)
        assert isinstance(reason, str) and reason, f"стенд {path!r} принят"


def case_t3_symlink_out():
    """§4.3 п.2: ссылка удаляется как ссылка, цель вне корня цела."""
    _, root, journal = stand_root()
    outside = new_stand()
    target = make_tree(outside / "цель")
    before = tree_hash(outside)
    (root / f"{WASTE}ссылка").symlink_to(target)
    entry = make_tree(root / f"{WASTE}внутри")
    (entry / "наружу").symlink_to(target)
    run_sweep(root, journal=journal)
    assert not os.path.lexists(root / f"{WASTE}ссылка"), "верхняя ссылка не удалена"
    assert not os.path.lexists(entry), "запись со ссылкой внутри не удалена"
    expect_intact(outside, before)


def case_t4a_other_fs():
    """§4.3 п.2: вложенный каталог с другим st_dev — отказ всей записи."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}фс")
    make_tree(entry / "другая-фс")
    before = tree_hash(entry)
    probe = lstat_substituting("другая-фс", st_dev=os.lstat(entry).st_dev + 1)
    run_sweep(root, journal=journal, lstat=probe)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "другая ФС")


def case_t4b_mount_inside():
    """§4.3 п.2: точка монтирования внутри записи (mountinfo стенда) — отказ."""
    stand, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}монт")
    point = entry / "точка"
    point.mkdir()
    mountinfo = stand / "mountinfo"
    mountinfo.write_text(
        "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
        f"99 22 0:77 / {point} rw,relatime shared:9 - tmpfs tmpfs rw\n", encoding="utf-8")
    before = tree_hash(entry)
    run_sweep(root, journal=journal, mountinfo=(str(mountinfo),))
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "монтирование внутри")


def case_t5_foreign_owner():
    """§4.3 п.3: ожидаемый uid не свой — отказ «чужой хозяин»."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}хозяин")
    before = tree_hash(entry)
    run_sweep(root, journal=journal, expected_uid=os.getuid() + 1)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "чужой хозяин")


def case_t6_euid_root():
    """§4.3 п.3: под euid 0 ход отказан целиком, 0 удалений."""
    stand, root, journal = stand_root()
    make_tree(root / f"{WASTE}рут")
    before = tree_hash(root)
    report, out = run_sweep(root, journal=journal, geteuid=lambda: 0)
    expect_intact(root, before)
    said = out + (Path(journal).read_text(encoding="utf-8") if journal.exists() else "")
    said += repr(report)
    assert "уборщик под root" in said, "нет причины «уборщик под root»"
    assert not [r for r in journal_rows(journal) if r.get("действие") == "удалено"]


def case_t7a_fresh_waste():
    """§4.3 п.4: свежий отход цел (срок отходов 6 ч)."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}свежий")
    before = tree_hash(entry)
    run_sweep(root, journal=journal, now=time.time())
    expect_intact(entry, before)
    expect_untouched(journal, entry.name)


def case_t7b_old_dir_fresh_file():
    """§4.3 п.4: возраст — по НОВЕЙШЕМУ узлу: старый верх, свежий файл в глубине.

    Срок 2 с из данных стенда: верх стареет на деле (ctime тоже), а глубокий
    файл моложе — правило «по верху» (mtime или max(mtime, ctime)) его удалит.
    """
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}старый-верх")
    time.sleep(2.6)
    (entry / "вложенный" / "свежий.txt").write_text("свежий\n", encoding="utf-8")
    before = tree_hash(entry)
    cfg = cfg_with_terms({"отходы_ночь": 2 / HOUR})
    run_sweep(root, journal=journal, now=time.time(), cfg=cfg)
    expect_intact(entry, before)
    expect_untouched(journal, entry.name)


def case_t8_utime_copy_is_fresh():
    """§4.3 п.4: копия с mtime в прошлом, ctime сейчас — свежая."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}копия")
    backdate(entry, 10 * DAY)
    before = tree_hash(entry)
    run_sweep(root, journal=journal, now=time.time())
    expect_intact(entry, before)
    expect_untouched(journal, entry.name)


def case_t9_cwd_holder():
    """§4.3 п.5: запись — cwd живого процесса."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}cwd")
    spawn(["sleep", "600"], cwd=str(entry / "вложенный"))
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "держит процесс")


def case_t10_fd_through_hardlink():
    """§4.3 п.5: fd на файл записи через жёсткую ссылку из ДРУГОГО каталога —
    строкой пути не поймать, только (dev, ino)."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}fd")
    elsewhere = new_stand()
    link = elsewhere / "ссылка-на-файл"
    os.link(entry / "файл-0.bin", link)
    proc = spawn([sys.executable, "-c", FD_HOLDER])
    proc.stdin.write(f"{link}\n")
    proc.stdin.flush()
    wait_word(proc)
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "держит процесс")


def case_t11_cmdline_holder():
    """§4.3 п.5: путь записи в командной строке живого процесса."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}cmdline")
    spawn([sys.executable, "-c", "import time; time.sleep(600)", str(entry)])
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "держит процесс")


def case_t12_environ_holder():
    """§4.3 п.5: TMPDIR процесса внутри записи."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}environ")
    spawn(["sleep", "600"], env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                 "TMPDIR": str(entry / "вложенный")})
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "держит процесс")


def case_t13_live_socket():
    """§4.3 п.6, Р-3: живой слушающий сокет — причина «живой сокет» (раньше узла)."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}s")
    socket_listener(entry / "s", relative=False)
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "живой сокет")


def case_t13b_relative_socket():
    """Р-3: сокет привязан ОТНОСИТЕЛЬНЫМ путём — /proc/net/unix пути не знает,
    запись держит правило S_ISSOCK по всему дереву: «узел-сокет»."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}r")
    socket_listener(entry / "вложенный" / "r", relative=True)
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "узел-сокет")


def case_t14_dead_socket_node():
    """§4.3 п.6: узел S_ISSOCK без слушателя — «узел-сокет»."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}d")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(entry / "вложенный" / "d"))
    sock.close()
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    expect_intact(entry, before)
    expect_refusal(journal_rows(journal), entry.name, "узел-сокет")


def case_t15_protected():
    """§4.3 п.7: защищённые имена целы при любом возрасте, строк в журнале нет."""
    _, night_root, night_journal = stand_root()
    _, press_root, press_journal = stand_root()
    for root in (night_root, press_root):
        make_tree(root / "claude-x")
        make_tree(root / "cc-socks")
    day_dir = press_root / QUARANTINE / time.strftime("%Y%m%d")
    day_dir.mkdir(parents=True)
    make_tree(day_dir / "0123456789ab", files=1)
    before = {root: tree_hash(root) for root in (night_root, press_root)}
    run_sweep(night_root, "ночь", journal=night_journal, now=time.time() + 30 * DAY)
    run_sweep(press_root, "давление", journal=press_journal, now=time.time() + 30 * DAY)
    for root, journal in ((night_root, night_journal), (press_root, press_journal)):
        expect_intact(root, before[root])
        assert journal_rows(journal) == [], f"строки журнала про защищённое: {journal.name}"


def quarantined_at(root: Path, hash12: str) -> Path:
    found = [p for p in (root / QUARANTINE).glob(f"*/{hash12}")
             if re.fullmatch(r"\d{8}", p.parent.name)]
    assert len(found) == 1, f"в карантине по {hash12} найдено {len(found)}"
    return found[0]


def quarantine_hash(rows: list[dict], name: str) -> str:
    acts = actions_for(rows, name)
    moved = [m.group(1) for a in acts if (m := re.fullmatch(r"в карантин ([0-9a-f]{12})", a))]
    assert len(moved) == 1, f"{name}: ждал одну строку «в карантин <хеш12>», есть {acts}"
    assert "удалено" not in acts, f"{name}: удалено вместо карантина"
    return moved[0]


def data_like_waste(root: Path, label: str) -> Path:
    """Отход с x.sql — единственный путь в карантин после M-4."""
    entry = make_tree(root / f"{WASTE}{label}")
    (entry / "вложенный" / "x.sql").write_text("select 1;\n", encoding="utf-8")
    return entry


def case_t16_quarantine_and_restore():
    """§4.3 п.8: отход с x.sql — в карантин по хешу, не удалён; restore()
    возвращает на прежнее имя с тем же деревом. (Неизвестное — случай M4.)"""
    _, root, journal = stand_root()
    data_like = data_like_waste(root, "sql")
    before = {data_like: tree_hash(data_like)}
    run_sweep(root, journal=journal)
    rows = journal_rows(journal)
    module = mod()
    for entry in (data_like,):
        hash12 = quarantine_hash(rows, entry.name)
        assert not os.path.lexists(entry), f"{entry.name}: осталась на прежнем имени"
        moved = quarantined_at(root, hash12)
        assert tree_hash(moved) == before[entry], f"{entry.name}: в карантине другое дерево"
        answer = module.restore(hash12, str(journal))
        assert answer is None or "возвращено" in str(answer), f"restore: {answer}"
        expect_intact(entry, before[entry])
        assert not os.path.lexists(moved), f"{entry.name}: копия осталась в карантине"


def case_t16b_restore_name_taken():
    """§4.2 --вернуть: прежнее имя занято (пустым каталогом — rename его молча
    заместил бы) — отказ с причиной, обе стороны целы."""
    _, root, journal = stand_root()
    entry = data_like_waste(root, "занято")
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    hash12 = quarantine_hash(journal_rows(journal), entry.name)
    moved = quarantined_at(root, hash12)
    entry.mkdir()
    answer = mod().restore(hash12, str(journal))
    assert isinstance(answer, str) and answer and "возвращено" not in answer, \
        f"занятое имя: ответ {answer!r}"
    assert tree_hash(moved) == before, "запись в карантине изменилась"
    assert entry.is_dir() and not any(entry.iterdir()), "занявший имя каталог тронут"


def case_t17_rename_race():
    """§4.3 п.9: держатель родился между rename и повторной проверкой —
    обратный rename, «возвращено», дерево цело."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}гонка")
    before = tree_hash(entry)
    calls = []

    def after_rename(*args, **kw):
        calls.append(args)
        for moved in (root / QUARANTINE).glob("*/*"):
            if moved.is_dir():
                spawn(["sleep", "600"], cwd=str(moved))

    run_sweep(root, journal=journal, after_rename=after_rename)
    assert calls, "крючок after_rename не позван — гонка не воспроизведена"
    expect_intact(entry, before)
    acts = actions_for(journal_rows(journal), entry.name)
    assert "возвращено" in acts, f"нет строки «возвращено»: {acts}"
    assert "удалено" not in acts, f"удалено при держателе: {acts}"


def case_t18_quarantine_leftovers():
    """§4.3 п.9: карантин старше 24 ч удаляет только ночной ход; моложе — цел;
    давление карантин не чистит при любом возрасте."""
    day_dir = time.strftime("%Y%m%d")
    stands = {}
    for label in ("ночь-25ч", "ночь-23ч", "давление-30сут"):
        _, root, journal = stand_root()
        (root / QUARANTINE / day_dir).mkdir(parents=True)
        item = make_tree(root / QUARANTINE / day_dir / "0123456789ab", files=1)
        # C-1: срок карантина судит только запись, названную строкой журнала.
        seed_quarantine_row(journal, root / f"{WASTE}прежнее", item, "отходы")
        stands[label] = (root, journal, item, tree_hash(item))
    root, journal, item, _ = stands["ночь-25ч"]
    run_sweep(root, "ночь", journal=journal, now=time.time() + 25 * HOUR)
    assert not os.path.lexists(item), "карантин старше 24 ч не удалён ночным ходом"
    root, journal, item, before = stands["ночь-23ч"]
    run_sweep(root, "ночь", journal=journal, now=time.time() + 23 * HOUR)
    expect_intact(item, before)
    root, journal, item, before = stands["давление-30сут"]
    run_sweep(root, "давление", journal=journal, now=time.time() + 30 * DAY)
    expect_intact(item, before)


def case_t19a_readonly_dir_inside():
    """§4.3 п.10: свой каталог 0555 с файлами внутри отхода — удалён целиком."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}ro")
    locked = entry / "только-чтение"
    locked.mkdir()
    for i in range(3):
        (locked / f"ф{i}").write_text("x", encoding="utf-8")
    locked.chmod(0o555)
    run_sweep(root, journal=journal)
    assert not os.path.lexists(entry), "запись с каталогом 0555 не удалена"
    assert not list(root.rglob("только-чтение")), "каталог 0555 остался в корне"


def case_t19b_foreign_dir_no_chmod():
    """§4.3 п.10: каталог чужого uid (подменён после rename) — chmod не зовётся,
    файлы целы, в журнале «осталось»."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}чужое")
    foreign = entry / "чужой"
    foreign.mkdir()
    for i in range(3):
        (foreign / f"ф{i}").write_text("x", encoding="utf-8")
    foreign.chmod(0o555)
    renamed = []
    probe = lstat_substituting("чужой", active=lambda: bool(renamed),
                               st_uid=os.getuid() + 1)
    run_sweep(root, journal=journal, lstat=probe,
              after_rename=lambda *a, **k: renamed.append(a))
    assert renamed, "крючок after_rename не позван"
    found = [p for p in root.rglob("чужой") if p.is_dir()]
    assert len(found) == 1, f"каталог «чужой» найден {len(found)} раз"
    assert stat.S_IMODE(os.lstat(found[0]).st_mode) == 0o555, "chmod позван на чужом"
    assert len(list(found[0].iterdir())) == 3, "файлы чужого каталога удалены"
    acts = actions_for(journal_rows(journal), entry.name)
    assert any("осталось" in a for a in acts), f"нет строки «осталось»: {acts}"


def case_t20_dry_run():
    """§7.3: сухой ход не меняет стенд и не пишет журнал; названное множество
    = множеству боевого хода того же стенда."""
    _, root, journal = stand_root()
    for name in (f"{WASTE}а", f"{WASTE}б", f"{WASTE}в", f"{UNKNOWN}д", "claude-x"):
        make_tree(root / name)
    data_like = make_tree(root / f"{WASTE}sql")
    (data_like / "x.sql").write_text("select 1;\n", encoding="utf-8")
    sock_entry = make_tree(root / f"{WASTE}сок")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(sock_entry / "s"))
    sock.close()
    now = time.time() + OLD
    before = tree_hash(root)
    report, _ = run_sweep(root, journal=journal, now=now, dry_run=True)
    expect_intact(root, before)
    assert not journal.exists(), "сухой ход создал журнал удалений"
    planned = {name for name, d in report.decisions.items() if d.action in ("удалить", "карантин")}
    # M-4: неизвестное («{UNKNOWN}д») не трогается — ни удалить, ни карантин.
    expected = {f"{WASTE}а", f"{WASTE}б", f"{WASTE}в", f"{WASTE}sql"}
    assert planned == expected, f"сухой назвал {sorted(planned)}"
    run_sweep(root, journal=journal, now=now)
    done = {os.path.basename(r["путь"]) for r in journal_rows(journal)
            if r.get("действие") == "удалено" or str(r.get("действие")).startswith("в карантин")}
    assert done == planned, f"боевой сделал {sorted(done)}, сухой назвал {sorted(planned)}"


SWEEP_IN_CHILD = textwrap.dedent("""\
    import importlib.util, sys, yaml
    mechanism, here, cfg_path, root, uid, journal, now = sys.argv[1:8]
    sys.path.insert(0, here)
    spec = importlib.util.spec_from_file_location("m", mechanism)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    m.sweep(root, "ночь", expected_uid=int(uid), holders=m.read_holders,
            journal=journal, cfg=cfg, now=float(now))
""")


def case_t21_no_sudo():
    """§4.1 сост. 8, §7.6: sudo отказал — шаг пропущен с причиной, 0 удалений, код 0."""
    mod()
    cfg = config_path()
    if cfg is None:
        raise Missing("нет tmp-sweep.yaml")
    stand, root, journal = stand_root()
    make_tree(root / f"{WASTE}без-права")
    bin_dir, mark = fake_sudo(stand)
    before = tree_hash(root)
    env = clean_env(PATH=f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    result = subprocess.run(
        [sys.executable, "-B", "-c", SWEEP_IN_CHILD, str(MECHANISM), str(HERE), str(cfg),
         str(root), str(os.getuid()), str(journal), str(time.time() + OLD)],
        env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"код {result.returncode}: {result.stderr[-300:]}"
    assert mark.exists(), "sudo из PATH не спрошен — права судились не им"
    assert "нет права видеть чужие процессы — шаг пропущен" in result.stdout, "нет причины"
    expect_intact(root, before)
    assert not [r for r in journal_rows(journal) if r.get("действие") == "удалено"]


def case_t22_full_disk_output():
    """§7.10: --selftest-full в /dev/full — код 0, стенд убран, удалено 20."""
    mod()
    stand = new_stand()
    note = stand / "записка"
    with open("/dev/full", "w") as full:
        result = run_cli("--selftest-full", env=clean_env(TMP_SWEEP_SELFTEST_NOTE=note),
                         stdout=full, timeout=300)
    assert result.returncode == 0, f"код {result.returncode}: {result.stderr[-300:]}"
    assert note.exists(), "записки о стенде нет — ход не дошёл до конца"
    lines = note.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].startswith(f"/tmp/{STAND_PREFIX}"), f"стенд: {lines[:1]}"
    assert not os.path.lexists(lines[0]), "стенд --selftest-full не убран"
    assert "удалено 20" in lines, f"записка: {lines}"


JOURNAL_FIELDS = {"когда", "путь", "мб", "возраст_ч", "правило", "класс", "действие"}


def case_t23_journal_form():
    """§4.2, §7.5: журнал 600, поля, МБ только st_nlink == 1; --сверить-журнал
    0 нарушений, вписанная строка вне /tmp — 1."""
    stand, root, _ = stand_root()
    logs = stand / "лог"
    logs.mkdir()
    journal = logs / "уборка-tmp.jsonl"
    plain = root / f"{WASTE}обычный"
    plain.mkdir()
    (plain / "м.bin").write_bytes(os.urandom(1 << 20))
    linked = root / f"{WASTE}жёсткая"
    linked.mkdir()
    (linked / "м.bin").write_bytes(os.urandom(1 << 20))
    outside = new_stand()
    os.link(linked / "м.bin", outside / "вторая-ссылка")
    run_sweep(root, journal=journal)
    assert stat.S_IMODE(os.stat(journal).st_mode) == 0o600, "режим журнала не 600"
    rows = journal_rows(journal)
    assert rows, "журнал пуст"
    for row in rows:
        missing = JOURNAL_FIELDS - set(row)
        assert not missing, f"в строке нет полей {sorted(missing)}"
    mb = {os.path.basename(r["путь"]): float(r["мб"]) for r in rows
          if r.get("действие") == "удалено"}
    assert 0.9 <= mb.get(plain.name, 0) <= 1.2, f"мб обычного {mb.get(plain.name)}"
    assert mb.get(linked.name, 9) < 0.1, f"мб жёсткой ссылки {mb.get(linked.name)}"
    env = clean_env(LOG_DIR=logs)
    result = run_cli("--сверить-журнал", env=env)
    assert result.returncode == 0, f"чистый журнал: код {result.returncode}"
    assert "нарушений 0" in result.stdout, f"чистый журнал: {result.stdout[-200:]}"
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({**rows[0], "путь": str(Path.home() / "чужое")},
                                ensure_ascii=False) + "\n")
    result = run_cli("--сверить-журнал", env=env)
    assert result.returncode == 1, f"строка вне /tmp: код {result.returncode}"
    assert "нарушений 1" in result.stdout, f"строка вне /tmp: {result.stdout[-200:]}"


def case_t24_holders_reader_secret():
    """§4.2: читатель печатает только /tmp-пути из cmdline и TMPDIR/TMP/TEMP;
    значение другой переменной — даже начинающееся с /tmp/ — секрет."""
    mod()
    stand = new_stand()
    spawn(["sleep", "600"], env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                 "TMPDIR": str(stand / "x"),
                                 "SECRET_TMP": "/tmp/секрет-q7Zx9",
                                 "SECRET": "abc-q7Zx9-plain"})
    result = run_cli("--держатели", env=clean_env())
    assert result.returncode == 0, f"код {result.returncode}: {result.stderr[-300:]}"
    data = json.loads(result.stdout)
    for key in ("inodes", "paths", "sockets", "mounts", "отказов"):
        assert key in data, f"в ответе читателя нет «{key}»"
    assert str(stand / "x") in data["paths"], "TMPDIR держателя не назван"
    assert "q7Zx9" not in result.stdout, "значение чужой переменной напечатано"


def case_journal_unwritable():
    """Р-7 контракта: журнал удалений не пишется (каталог, /dev/full) —
    запись НЕ удаляется и НЕ переносится."""
    for label in ("каталог", "/dev/full"):
        stand, root, _ = stand_root()
        journal = Path("/dev/full") if label == "/dev/full" else stand / "журнал-каталог"
        if label == "каталог":
            journal.mkdir()
        entries = [make_tree(root / f"{WASTE}ж"), make_tree(root / f"{UNKNOWN}ж")]
        before = {p: tree_hash(p) for p in entries}
        run_sweep(root, journal=journal)
        for entry in entries:
            assert os.path.lexists(entry), f"{label}: {entry.name} ушла без строки журнала"
            expect_intact(entry, before[entry])


def case_odd_names():
    """Имена с \\n и U+2028: удалены, журнал режется только по \\n, имён в stdout нет."""
    _, root, journal = stand_root()
    # метки qz7/qz8 — латиница: repr() экранирует \n и U+2028, а метку — нет
    names = [f"{WASTE}qz7\nb", f"{WASTE}qz8\u2028d"]
    for name in names:
        make_tree(root / name)
    _, out = run_sweep(root, journal=journal)
    rows = journal_rows(journal)
    deleted = {os.path.basename(r["путь"]) for r in rows if r.get("действие") == "удалено"}
    assert deleted == set(names), f"удалены {deleted!r}"
    assert not [n for n in names if os.path.lexists(root / n)], "записи на месте"
    for marker in ("qz7", "qz8"):
        assert marker not in out, f"имя записи ({marker}) в stdout хода"


def case_deep_nesting():
    """Вложенность глубже PATH_MAX: удаление fd-обходом доходит до дна."""
    _, root, journal = stand_root()
    entry = root / f"{WASTE}глубь"
    entry.mkdir()
    component = "к" * 30
    fds = [os.open(entry, NO_DIR_FOLLOW)]
    try:
        for _ in range(200):
            os.mkdir(component, dir_fd=fds[-1])
            fds.append(os.open(component, NO_DIR_FOLLOW, dir_fd=fds[-1]))
        fd = os.open("дно.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fds[-1])
        os.write(fd, b"bottom")
        os.close(fd)
    finally:
        for fd in fds:
            os.close(fd)
    assert tree_hash(entry)[1] == 202, "стенд глубины не построен"
    run_sweep(root, journal=journal)
    assert not os.path.lexists(entry), "глубокая запись не на прежнем имени"
    left = [n for n in names_under(root) if n in (component, "дно.txt")]
    assert not left, f"глубокое дерево не удалено до дна: осталось узлов {len(left)}"
    assert "удалено" in actions_for(journal_rows(journal), entry.name)


def case_lock_second_instance():
    """Р-4 контракта: лок tmp-sweep занят — --вернуть не идёт поперёк (код 0,
    запись в карантине); лок свободен — возврат проходит."""
    stand, root, _ = stand_root()
    logs = stand / "лог"
    (logs / "locks" / "services").mkdir(parents=True)
    journal = logs / "уборка-tmp.jsonl"
    entry = data_like_waste(root, "лок")
    before = tree_hash(entry)
    run_sweep(root, journal=journal)
    hash12 = quarantine_hash(journal_rows(journal), entry.name)
    moved = quarantined_at(root, hash12)
    env = clean_env(LOG_DIR=logs)
    lock_fd = os.open(logs / "locks" / "services" / "tmp-sweep.lock",
                      os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = run_cli("--вернуть", hash12, env=env, timeout=30)
    finally:
        os.close(lock_fd)
    assert busy.returncode == 0, f"лок занят: код {busy.returncode}"
    assert os.path.lexists(moved) and not os.path.lexists(entry), \
        "при занятом локе запись вернулась — второй экземпляр пошёл"
    free = run_cli("--вернуть", hash12, env=env, timeout=30)
    assert free.returncode == 0, f"лок свободен: код {free.returncode}: {free.stderr[-200:]}"
    assert "возвращено" in free.stdout, f"лок свободен: {free.stdout[-200:]}"
    expect_intact(entry, before)


def case_tr_registry():
    """Р-2: каждый префикс отходов — ровно префикс mkdtemp какой-то пробы дерева;
    dognat/cdp — не отходы (дерево разбора конфликта); реестр не пуст."""
    cfg = load_cfg()
    waste = cfg.get("отходы_проб") or []
    assert waste, "реестр отходов пуст — проба ничего бы не судила"
    creators: dict[str, list[str]] = {}
    for probe in sorted(HERE.glob("test_*.py")):
        text = probe.read_text(encoding="utf-8", errors="replace")
        for prefix in re.findall(r"mkdtemp\(\s*prefix\s*=\s*[\"']([^\"']+)[\"']", text):
            creators.setdefault(prefix, []).append(probe.name)
    assert creators, "в дереве не найдено ни одного mkdtemp(prefix=…) проб"
    bad = [p for p in waste if p.startswith(("dognat", "cdp"))]
    assert not bad, f"в реестре отходов работа, а не отходы: {bad}"
    no_dash = [p for p in waste if not p.endswith("-")]
    assert not no_dash, f"префиксы без дефиса ловят чужое: {no_dash}"
    orphan = [p for p in waste if p not in creators]
    assert not orphan, f"у префиксов нет создателя-пробы: {orphan}"
    assert WASTE in waste, f"{WASTE} — отход, на котором стоят пробы, не в реестре"
    assert not any(UNKNOWN.startswith(p) for p in waste), f"{UNKNOWN} попал в реестр"
    protected = cfg.get("защищено") or []
    for name in ("claude-", "cc-socks", QUARANTINE):
        assert name in protected, f"«{name}» не в защищено"
    assert ".sql" in (cfg.get("похоже_на_данные") or []), "«.sql» не в похоже_на_данные"
    terms = cfg.get("сроки_ч") or {}
    for key in ("отходы_ночь", "отходы_давление", "неизвестное_ночь",
                "неизвестное_давление", "карантин"):
        assert key in terms, f"нет срока «{key}»"


# ─────────────────────── круг 1: решения смены по ревью ──────────────────────

class Killed(BaseException):
    """SIGKILL посреди хода: `except Exception` механизма его не ловит."""


def seed_quarantine_row(journal: Path, original: Path, item: Path, klass: str) -> None:
    """Строка «в карантин <хеш12>», какой её пишет ход: запись карантина с именем."""
    row = {"когда": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "путь": str(original), "мб": 0.0,
           "возраст_ч": 0.0, "правило": "отход пробы" if klass == "отходы" else "неизвестное",
           "класс": klass, "действие": f"в карантин {item.name}", "куда": str(item),
           "было_имя": original.name}
    fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def quarantine_items(root: Path) -> list[Path]:
    return [p for p in (root / QUARANTINE).glob("*/*") if re.fullmatch(r"\d{8}", p.parent.name)]


def rows_naming(rows: list[dict], hash12: str) -> list[dict]:
    return [r for r in rows if hash12 in json.dumps(r, ensure_ascii=False)]


def into_quarantine(src, dst) -> bool:
    return QUARANTINE in os.fsdecode(dst) and QUARANTINE not in os.fsdecode(src)


def case_c1_killed_at_rename():
    """C-1: ход убит на rename в карантин — строка-намерение с хешем уже в журнале;
    запись цела на прежнем имени, следующий ход её доводит без падения."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}убит")
    before = tree_hash(entry)
    real_rename, hit = os.rename, []

    def kill_at_rename(src, dst, *args, **kw):
        if into_quarantine(src, dst):
            hit.append((os.path.basename(os.fsdecode(src)), os.path.basename(os.fsdecode(dst))))
            raise Killed()
        return real_rename(src, dst, *args, **kw)

    os.rename = kill_at_rename
    try:
        try:
            run_sweep(root, journal=journal)
        except Killed:
            pass
    finally:
        os.rename = real_rename
    assert hit, "rename в карантин не замечен — ход перенёс запись другой дверью"
    name, hash12 = hit[0]
    named = [r for r in rows_naming(journal_rows(journal), hash12)
             if os.path.basename(str(r.get("путь", ""))) == name]
    assert named, (f"ход убит на rename: в журнале нет строки с хешем {hash12} и именем "
                   f"{name} — намерение не записано ДО rename")
    expect_intact(entry, before)
    answer = mod().restore(hash12, str(journal))
    assert isinstance(answer, str) and "возвращено" not in answer, \
        f"возврат несостоявшегося переноса: {answer!r}"
    run_sweep(root, journal=journal, now=time.time() + OLD + HOUR)
    assert not os.path.lexists(entry), "следующий ход не довёл запись"
    assert "удалено" in actions_for(journal_rows(journal), entry.name)


def case_c1_killed_after_rename():
    """C-1: ход убит после rename — каждая запись карантина названа строкой
    журнала (хеш + прежнее имя): отход с x.sql возвращается по хешу, отход без
    данных ночью удаляется по сроку."""
    _, root, journal = stand_root()
    sql = data_like_waste(root, "а-sql")
    plain = make_tree(root / f"{WASTE}б-отход")
    before_sql = tree_hash(sql)

    def kill(*args, **kw):
        raise Killed()

    killed = False
    try:
        run_sweep(root, journal=journal, after_rename=kill)
    except Killed:
        killed = True
    assert killed, "after_rename не позван — убийство после rename не воспроизведено"
    items = quarantine_items(root)
    assert items, "после rename в карантине пусто"
    rows = journal_rows(journal)
    owner = {}
    for item in items:
        for row in rows_naming(rows, item.name):
            base = os.path.basename(str(row.get("путь", "")))
            if base in (sql.name, plain.name):
                owner[item.name] = base
    nameless = [i.name for i in items if i.name not in owner]
    assert not nameless, (f"ход убит после rename: в карантине {len(nameless)} из "
                          f"{len(items)} записей без строки журнала — прежние имена потеряны")
    by_name = {base: h for h, base in owner.items()}
    if sql.name in by_name:
        answer = mod().restore(by_name[sql.name], str(journal))
        assert answer is None or "возвращено" in str(answer), f"restore: {answer!r}"
        expect_intact(sql, before_sql)
    if plain.name in by_name:
        plain_item = [i for i in items if i.name == by_name[plain.name]][0]
        run_sweep(root, "ночь", journal=journal, now=time.time() + 30 * DAY)
        assert not os.path.lexists(plain_item), "отход в карантине со строкой не удалён по сроку"


def case_c1_nameless_quarantine_kept():
    """C-1: запись карантина без строки журнала не удаляется никогда (+30 сут),
    итог называет число; пара со строкой (класс «отходы») удаляется по сроку."""
    _, root, journal = stand_root()
    day = root / QUARANTINE / time.strftime("%Y%m%d")
    day.mkdir(parents=True)
    nameless = make_tree(day / "cccccccccccc", files=1)
    named = make_tree(day / "dddddddddddd", files=1)
    seed_quarantine_row(journal, root / f"{WASTE}со-строкой", named, "отходы")
    before = tree_hash(nameless)
    _, out = run_sweep(root, "ночь", journal=journal, now=time.time() + 30 * DAY)
    gone = [r for r in journal_rows(journal)
            if r.get("действие") == "удалено" and nameless.name in str(r.get("путь"))]
    assert tree_hash(nameless) == before, (
        f"запись карантина без строки журнала удалена по сроку (строк «удалено» о ней: "
        f"{len(gone)})")
    assert re.search(r"без строки журнала:\s*1\b", out), \
        f"итог не назвал число записей карантина без строки: {out[-300:]!r}"
    assert not os.path.lexists(named), "запись карантина со строкой не удалена — пара не здорова"


def case_c1_vanished_after_rename():
    """C-1 п.3: запись исчезла между rename и перепроверкой — ход не падает,
    второй отход доводится, в карантине ничего не застряло."""
    _, root, journal = stand_root()
    make_tree(root / f"{WASTE}а-исчезнет")
    second = make_tree(root / f"{WASTE}б-остаётся")
    gone = []

    def after_rename(paths, *args, **kw):
        if not gone and paths:
            force_remove(Path(paths[0]))
            gone.append(paths[0])

    try:
        run_sweep(root, journal=journal, after_rename=after_rename)
    except Exception as exc:  # noqa: BLE001 — падение хода и есть находка
        raise AssertionError(f"запись исчезла после rename — ход упал: "
                             f"{type(exc).__name__}: {exc}") from exc
    assert gone, "after_rename не позван — исчезновение не воспроизведено"
    stranded = quarantine_items(root)
    assert not stranded, f"в карантине застряли записи: {len(stranded)}"
    assert not os.path.lexists(second), f"{second.name} не удалён"
    assert "удалено" in actions_for(journal_rows(journal), second.name)


def case_m1_switch_in_script():
    """M-1: прямой `tmp-sweep.py --ночь/--давление` при TMP_SWEEP_ENABLED не 1 —
    код 2 и причина ДО лока и держателей; при 1 (и при --dry-run) идёт дальше.
    Страховка: лок занят пробой, sudo — заглушка; дальше лока ход не идёт ни в
    одном варианте, боевой /tmp не читается."""
    mod()
    stand = new_stand()
    bin_dir, mark = fake_sudo(stand)
    home = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir=Path.home()))
    CREATED.append(home)
    logs = home / "лог"
    (logs / "locks" / "services").mkdir(parents=True)
    conf = home / "harness.conf"
    path_env = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    def call(conf_text, args, **env_extra):
        if conf_text is None:
            conf.unlink(missing_ok=True)
        else:
            conf.write_text(conf_text, encoding="utf-8")
        env = clean_env(LOG_DIR=logs, PATH=path_env, HARNESS_CONF=conf, **env_extra)
        result = run_cli(*args, env=env, timeout=60)
        return result.returncode, result.stdout + result.stderr

    off = [("ключ 0", 'TMP_SWEEP_ENABLED="0"\n', {}),
           ("нет ключа", 'DISK_ALERT_PCT="80"\n', {}),
           ("пусто", 'TMP_SWEEP_ENABLED=""\n', {}),
           ("нет файла", None, {}),
           ("окружение 0 старше файла 1", 'TMP_SWEEP_ENABLED="1"\n', {"TMP_SWEEP_ENABLED": "0"})]
    on = [("ключ 1", ("--ночь",), 'TMP_SWEEP_ENABLED="1"\n', {}),
          ("окружение 1 старше файла 0", ("--ночь",), 'TMP_SWEEP_ENABLED="0"\n',
           {"TMP_SWEEP_ENABLED": "1"}),
          ("ключ 0, сухой ход", ("--ночь", "--dry-run"), 'TMP_SWEEP_ENABLED="0"\n', {})]
    lock_fd = os.open(logs / "locks" / "services" / "tmp-sweep.lock", os.O_RDWR | os.O_CREAT,
                      0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for label, args, text, extra in on:
            code, said = call(text, args, **extra)
            assert "лок tmp-sweep занят" in said, \
                f"{label}: выключатель не пустил включённый ход (код {code}): {said[-200:]!r}"
        for label, text, extra in off:
            for mode in ("--ночь", "--давление"):
                code, said = call(text, (mode,), **extra)
                assert "TMP_SWEEP_ENABLED" in said and code == 2, (
                    f"{label} {mode}: сам скрипт не судит выключатель (код {code}, "
                    f"{'дошёл до лока' if 'лок tmp-sweep занят' in said else said[-150:]!r})")
    finally:
        os.close(lock_fd)
    assert not mark.exists(), "держателей спросили — ход пошёл дальше выключателя"
    assert not list(logs.rglob("уборка-tmp.jsonl")), "журнал удалений создан при выключенном"


BANNED_WASTE_NAMES = ("больной-указаний", "след-инцидента", "входящие-копия", "класс-x9")


def case_m2_word_prefixes():
    """M-2: префиксы-слова («больной-», «след-», «входящие-», «класс-») не
    отходы: `/tmp/больной-указаний` и копии улик целы; отход-пара удалён."""
    _, root, journal = stand_root()
    entries = [make_tree(root / name) for name in BANNED_WASTE_NAMES]
    before = {e: tree_hash(e) for e in entries}
    pair = make_tree(root / f"{WASTE}пара")
    run_sweep(root, journal=journal, now=time.time() + OLD)
    rows = journal_rows(journal)
    touched = {e.name: [a for a in actions_for(rows, e.name)
                        if a == "удалено" or a.startswith("в карантин")] for e in entries}
    touched = {k: v for k, v in touched.items() if v}
    waste = load_cfg().get("отходы_проб") or []
    words = sorted({p for p in waste for n in BANNED_WASTE_NAMES if n.startswith(p)})
    assert not touched and not words, (
        f"запись по префиксу-слову без доказанного создателя тронута: {touched}; "
        f"такие префиксы в реестре отходов: {words}")
    for entry in entries:
        expect_intact(entry, before[entry])
    assert not os.path.lexists(pair), "отход-пара не удалён — ход не шёл"


DEEP = 1200


def build_chain(entry: Path, depth: int, component: str = "к") -> None:
    """Цепочка каталогов глубиной depth, один fd открыт в каждый момент."""
    entry.mkdir()
    fd = os.open(entry, NO_DIR_FOLLOW)
    try:
        for _ in range(depth):
            os.mkdir(component, dir_fd=fd)
            nxt = os.open(component, NO_DIR_FOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        bottom = os.open("дно.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
        os.write(bottom, b"bottom")
        os.close(bottom)
    finally:
        os.close(fd)


def chain_bottom_exists(entry: Path, depth: int, component: str = "к") -> bool:
    try:
        fd = os.open(entry, NO_DIR_FOLLOW)
    except OSError:
        return False
    try:
        for _ in range(depth):
            nxt = os.open(component, NO_DIR_FOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        os.stat("дно.txt", dir_fd=fd, follow_symlinks=False)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def case_m3_depth_1200():
    """M-3: запись глубиной 1200 — «отказ: слишком глубоко», дно цело; соседний
    отход удалён, ход не падает."""
    _, root, journal = stand_root()
    deep = root / f"{WASTE}глубина"
    build_chain(deep, DEEP)
    near = make_tree(root / f"{WASTE}рядом")
    try:
        run_sweep(root, journal=journal)
    except RecursionError as exc:
        raise AssertionError(f"глубина {DEEP} уронила ход: RecursionError; соседний отход "
                             f"{'цел' if os.path.lexists(near) else 'удалён'}") from exc
    assert chain_bottom_exists(deep, DEEP), "глубокая запись тронута"
    assert not os.path.lexists(near), "соседний отход не удалён"
    expect_refusal(journal_rows(journal), deep.name, "слишком глубоко")


def case_m4_unknown_never_removed():
    """M-4: «неизвестное» (и с x.sql) цело через год в обоих режимах, строк
    переноса/удаления нет; строка карантина класса «неизвестное» не удаляется
    по сроку. Пары: отход удалён, отход в карантине со строкой удалён."""
    for mode in ("ночь", "давление"):
        _, root, journal = stand_root()
        plain = make_tree(root / f"{UNKNOWN}м4")
        data = make_tree(root / f"{UNKNOWN}м4-sql")
        (data / "x.sql").write_text("select 1;\n", encoding="utf-8")
        pair = make_tree(root / f"{WASTE}м4-пара")
        before = {e: tree_hash(e) for e in (plain, data)}
        report, _ = run_sweep(root, mode, journal=journal, now=time.time() + 365 * DAY)
        rows = journal_rows(journal)
        for entry in (plain, data):
            moved = [a for a in actions_for(rows, entry.name)
                     if a == "удалено" or a.startswith("в карантин")]
            assert not moved, f"{mode}: «неизвестное» {entry.name} тронуто: {moved}"
            decision = report.decisions.get(entry.name)
            assert decision is None or decision.action not in ("удалить", "карантин"), \
                f"{mode}: решение по «неизвестному» {entry.name}: {decision.action}"
            expect_intact(entry, before[entry])
        assert not os.path.lexists(pair), f"{mode}: отход-пара не удалён — ход не шёл"
    _, root, journal = stand_root()
    day = root / QUARANTINE / time.strftime("%Y%m%d")
    day.mkdir(parents=True)
    unknown_item = make_tree(day / "aaaaaaaaaaaa", files=1)
    waste_item = make_tree(day / "bbbbbbbbbbbb", files=1)
    seed_quarantine_row(journal, root / f"{UNKNOWN}в-карантине", unknown_item, "неизвестное")
    seed_quarantine_row(journal, root / f"{WASTE}в-карантине", waste_item, "отходы")
    before = tree_hash(unknown_item)
    run_sweep(root, "ночь", journal=journal, now=time.time() + 30 * DAY)
    assert tree_hash(unknown_item) == before, \
        "«неизвестное» в карантине удалено по сроку без слова владельца"
    assert not os.path.lexists(waste_item), "отход в карантине со строкой не удалён — пара не здорова"


MMAP_HOLDER = textwrap.dedent("""\
    import mmap, sys, time
    with open(sys.stdin.readline().rstrip("\\n"), "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    print("ok", flush=True)
    time.sleep(600)
""")


def case_m1_exe_and_mmap_holders():
    """ревью m1: процесс, запущенный из файла записи (cwd вне, fd нет, в cmdline
    пути нет), и процесс с файлом записи в mmap (fd закрыт) — держатели."""
    _, root, journal = stand_root()
    exe_entry = make_tree(root / f"{WASTE}exe")
    binary = exe_entry / "вложенный" / "спящий"
    shutil.copy2(shutil.which("sleep"), binary)
    binary.chmod(0o755)
    spawn(["sleep", "600"], executable=str(binary))
    map_entry = make_tree(root / f"{WASTE}mmap")
    proc = spawn([sys.executable, "-c", MMAP_HOLDER])
    proc.stdin.write(f"{map_entry / 'файл-0.bin'}\n")
    proc.stdin.flush()
    wait_word(proc)
    before = {e: tree_hash(e) for e in (exe_entry, map_entry)}
    run_sweep(root, journal=journal)
    rows = journal_rows(journal)
    for entry in (exe_entry, map_entry):
        expect_refusal(rows, entry.name, "держит процесс")
        expect_intact(entry, before[entry])


def case_m3_restore_refusal_in_words():
    """ревью m3: rename при возврате отказан (EACCES) — ответ словами, не трасса;
    запись в карантине цела."""
    _, root, journal = stand_root()
    entry = data_like_waste(root, "возврат")
    run_sweep(root, journal=journal)
    hash12 = quarantine_hash(journal_rows(journal), entry.name)
    moved = quarantined_at(root, hash12)
    before = tree_hash(moved)
    root.chmod(0o555)
    try:
        try:
            answer = mod().restore(hash12, str(journal))
        except OSError as exc:
            raise AssertionError(f"restore при отказе rename упал трассой: "
                                 f"{type(exc).__name__}: {exc}") from exc
    finally:
        root.chmod(0o700)
    assert isinstance(answer, str) and answer and "возвращено" not in answer, \
        f"отказ rename: ответ {answer!r}"
    assert tree_hash(moved) == before, "запись в карантине изменилась"


def case_m5_left_not_counted():
    """ревью m5: запись удалена не до конца («осталось узлов») — в Report.deleted
    её нет."""
    _, root, journal = stand_root()
    entry = make_tree(root / f"{WASTE}недоудалено")
    foreign = entry / "чужой"
    foreign.mkdir()
    (foreign / "ф").write_text("x", encoding="utf-8")
    foreign.chmod(0o555)
    renamed = []
    probe = lstat_substituting("чужой", active=lambda: bool(renamed), st_uid=os.getuid() + 1)
    report, _ = run_sweep(root, journal=journal, lstat=probe,
                          after_rename=lambda *a, **k: renamed.append(a))
    assert renamed, "крючок after_rename не позван"
    acts = actions_for(journal_rows(journal), entry.name)
    assert any("осталось" in a for a in acts), f"частичное удаление не воспроизведено: {acts}"
    assert report.deleted == 0, f"недоудалённое посчитано удалённым: Report.deleted={report.deleted}"


CASES = [
    ("T1", case_t1_twenty_dead_waste),
    ("T2", case_t2_cli_root_substitution),
    ("T2e", case_t2e_check_root_form),
    ("T3", case_t3_symlink_out),
    ("T4a", case_t4a_other_fs),
    ("T4b", case_t4b_mount_inside),
    ("T5", case_t5_foreign_owner),
    ("T6", case_t6_euid_root),
    ("T7a", case_t7a_fresh_waste),
    ("T7b", case_t7b_old_dir_fresh_file),
    ("T8", case_t8_utime_copy_is_fresh),
    ("T9", case_t9_cwd_holder),
    ("T10", case_t10_fd_through_hardlink),
    ("T11", case_t11_cmdline_holder),
    ("T12", case_t12_environ_holder),
    ("T13", case_t13_live_socket),
    ("T13b", case_t13b_relative_socket),
    ("T14", case_t14_dead_socket_node),
    ("T15", case_t15_protected),
    ("T16", case_t16_quarantine_and_restore),
    ("T16b", case_t16b_restore_name_taken),
    ("T17", case_t17_rename_race),
    ("T18", case_t18_quarantine_leftovers),
    ("T19a", case_t19a_readonly_dir_inside),
    ("T19b", case_t19b_foreign_dir_no_chmod),
    ("T20", case_t20_dry_run),
    ("T21", case_t21_no_sudo),
    ("T22", case_t22_full_disk_output),
    ("T23", case_t23_journal_form),
    ("T24", case_t24_holders_reader_secret),
    ("Т-журнал", case_journal_unwritable),
    ("Т-имена", case_odd_names),
    ("Т-глубина", case_deep_nesting),
    ("Т-лок", case_lock_second_instance),
    ("T-R", case_tr_registry),
    ("C1-убит-на-rename", case_c1_killed_at_rename),
    ("C1-убит-после-rename", case_c1_killed_after_rename),
    ("C1-без-строки", case_c1_nameless_quarantine_kept),
    ("C1-исчезла", case_c1_vanished_after_rename),
    ("M1-выключатель", case_m1_switch_in_script),
    ("M2-слова", case_m2_word_prefixes),
    ("M3-глубина-1200", case_m3_depth_1200),
    ("M4-неизвестное", case_m4_unknown_never_removed),
    ("m1-exe-mmap", case_m1_exe_and_mmap_holders),
    ("m3-возврат-словами", case_m3_restore_refusal_in_words),
    ("m5-осталось", case_m5_left_not_counted),
]


def cleanup_case() -> None:
    kill_procs()
    while CREATED:
        force_remove(CREATED.pop())


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    chosen = set(argv[1:])
    unknown = chosen - {cid for cid, _ in CASES}
    if unknown:
        print(f"нет таких случаев: {sorted(unknown)}")
        return 2
    print(f"прежних стендов убрано: {clean_stale_stands()}", flush=True)
    failures = 0
    total = 0
    for cid, case in CASES:
        if chosen and cid not in chosen:
            continue
        total += 1
        try:
            case()
            print(f"ок     {cid}", flush=True)
        except Missing as exc:
            failures += 1
            print(f"ПРОВАЛ {cid}: {exc}", flush=True)
        except AssertionError as exc:
            failures += 1
            print(f"ПРОВАЛ {cid}: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001 — падение механизма тоже красное случая
            failures += 1
            print(f"ПРОВАЛ {cid}: {type(exc).__name__}: {exc}", flush=True)
        finally:
            cleanup_case()
    print(f"УБОРКА TMP: случаев {total}, неудач {failures}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
