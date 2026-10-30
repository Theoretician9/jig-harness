#!/usr/bin/env python3
"""Уборка /tmp: отходы проб удаляются, неизвестное уходит в карантин на 24 ч.

Механизм УДАЛЯЕТ (И-1): запись трогается только после rename в карантин и
повторной проверки держателей; журнал удалений — условие необратимого шага.
Данные (префиксы, сроки, защищённые имена) — tmp-sweep.yaml.
"""
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = "/tmp"
QUARANTINE = ".уборка-карантин"
HERE = Path(__file__).resolve().parent
HOLDER_ENV = ("TMPDIR", "TMP", "TEMP")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Обход рекурсивен: предел питона 1000 кадров, а одна глубокая запись в /tmp иначе
# роняла бы весь ход каждую ночь. Глубже — отказ этой записи, ход идёт дальше.
MAX_DEPTH = 400


def say(text: str) -> None:
    # Закрытый stdout (>/dev/full, обрыв канала) не должен ронять ход посреди удаления.
    try:
        print(text, flush=True)
    except OSError:
        sys.stdout = open(os.devnull, "w")


@dataclass
class Holders:
    inodes: set
    paths: list
    sockets: list


@dataclass
class NoRight:
    reason: str


@dataclass
class Facts:
    newest: float = 0.0
    inodes: set = field(default_factory=set)
    live_socket: bool = False
    socket_node: bool = False
    foreign_uid: bool = False
    other_dev: bool = False
    mount_inside: bool = False
    unreadable: bool = False
    too_deep: bool = False
    vanished: bool = False
    held: bool = False
    data_like: bool = False
    size_unique_mb: float = 0.0


@dataclass
class Decision:
    klass: str
    rule: str
    action: str


@dataclass
class Report:
    decisions: dict = field(default_factory=dict)
    deleted: int = 0
    quarantined: int = 0
    refused: int = 0
    nameless: int = 0
    skipped: str | None = None


# --- держатели ---------------------------------------------------------------

def _unescape(raw: bytes) -> str:
    return os.fsdecode(re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m.group(1), 8)]), raw))


def read_mounts(files) -> list[str]:
    points = []
    for name in files:
        try:
            data = Path(name).read_bytes()
        except OSError:
            continue
        for line in data.split(b"\n"):
            parts = line.split(b" ")
            if len(parts) > 4:
                points.append(_unescape(parts[4]))
    return points


def read_unix_sockets() -> list[str]:
    paths = []
    with open("/proc/net/unix", "rb") as handle:
        next(handle)
        for line in handle:
            parts = line.rstrip(b"\n").split(None, 7)
            if len(parts) == 8 and parts[7].startswith(b"/"):
                paths.append(os.fsdecode(parts[7]))
    return paths


def _process_inodes(base: str) -> tuple[set, bool]:
    # exe и map_files: бинарник из записи и отображённый в память файл держат
    # запись при cwd вне /tmp и закрытых fd.
    links = ["cwd", "root", "exe"]
    denied = False
    for folder in ("fd", "map_files"):
        try:
            links += [f"{folder}/{name}" for name in os.listdir(f"{base}/{folder}")]
        except OSError:
            denied = True
    found = set()
    for link in links:
        try:
            st = os.stat(f"{base}/{link}")
        except OSError:
            continue
        found.add((st.st_dev, st.st_ino))
    return found, denied


def _process_paths(base: str) -> tuple[set, bool]:
    found, denied = set(), False
    try:
        for arg in Path(f"{base}/cmdline").read_bytes().split(b"\0"):
            text = os.fsdecode(arg)
            if text.startswith(ROOT + "/"):
                found.add(text)
    except OSError:
        denied = True
    try:
        # Только значения путевых переменных: остальное окружение — секреты, в вывод не идёт.
        for pair in Path(f"{base}/environ").read_bytes().split(b"\0"):
            key, _, value = pair.partition(b"=")
            text = os.fsdecode(value)
            if os.fsdecode(key) in HOLDER_ENV and text.startswith(ROOT + "/"):
                found.add(text)
    except OSError:
        denied = True
    return found, denied


def collect_holders() -> dict:
    inodes, paths, refused = set(), set(), 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = f"/proc/{pid}"
        got_inodes, denied_fd = _process_inodes(base)
        got_paths, denied_args = _process_paths(base)
        inodes |= got_inodes
        paths |= got_paths
        refused += denied_fd + denied_args
    sockets = [p for p in read_unix_sockets() if p.startswith(ROOT + "/")]
    mounts = [p for p in read_mounts(("/proc/self/mountinfo", "/proc/1/mountinfo"))
              if p.startswith(ROOT + "/")]
    return {"inodes": sorted(inodes), "paths": sorted(paths), "sockets": sockets,
            "mounts": mounts, "отказов": refused}


def read_holders():
    # Чужие /proc/<pid>/fd читает только root; уборщик сам без root (см. sweep).
    cmd = ["sudo", "-n", sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--держатели"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return NoRight(str(exc))
    if result.returncode:
        return NoRight(f"sudo -n: код {result.returncode}")
    try:
        data = json.loads(result.stdout)
        return Holders({tuple(x) for x in data["inodes"]}, data["paths"], data["sockets"])
    except (ValueError, KeyError, TypeError):
        return NoRight("ответ читателя не JSON")


# --- факты и суд ---------------------------------------------------------------

def entry_facts(dir_fd, name, *, root_dev, expected_uid, holders, lstat, entry_path,
                mounts, data_suffixes, skip_top_ctime=False) -> Facts:
    """Обход записи fd-ами: симлинк не разворачивается, чужая ФС не пересекается."""
    facts = Facts()

    def visit(parent_fd, node, depth):
        top = depth == 0
        try:
            st = lstat(node, dir_fd=parent_fd)
        except FileNotFoundError:
            facts.vanished |= top
            return
        # ctime верхнего узла после rename сдвигается самим rename — он не «свежесть».
        newest = st.st_mtime if (top and skip_top_ctime) else max(st.st_mtime, st.st_ctime)
        facts.newest = max(facts.newest, newest)
        facts.inodes.add((st.st_dev, st.st_ino))
        facts.foreign_uid |= st.st_uid != expected_uid
        if st.st_dev != root_dev:
            facts.other_dev = True
            return
        if stat.S_ISSOCK(st.st_mode):
            facts.socket_node = True
        elif stat.S_ISREG(st.st_mode):
            facts.data_like |= node.endswith(data_suffixes)
            # Жёсткая ссылка: байты принадлежат не только этой записи.
            if st.st_nlink == 1:
                facts.size_unique_mb += st.st_size / 1048576
        elif stat.S_ISDIR(st.st_mode):
            if depth >= MAX_DEPTH:
                facts.too_deep = True
                return
            try:
                fd = os.open(node, DIR_FLAGS, dir_fd=parent_fd)
            except OSError:
                facts.unreadable = True
                return
            try:
                for child in os.listdir(fd):
                    visit(fd, child, depth + 1)
            except PermissionError:
                facts.unreadable = True
            finally:
                os.close(fd)

    visit(dir_fd, name, 0)

    def inside(path):
        return path == entry_path or path.startswith(entry_path + "/")

    facts.mount_inside = any(inside(p) for p in mounts)
    facts.live_socket = any(inside(p) for p in holders.sockets)
    facts.held = bool(facts.inodes & holders.inodes) or any(inside(p) for p in holders.paths)
    return facts


# Порядок суда: первая сработавшая строка — причина отказа.
REFUSALS = (
    ("foreign_uid", "чужой хозяин"),
    ("other_dev", "другая ФС"),
    ("mount_inside", "монтирование внутри"),
    ("unreadable", "нечитаемо"),
    ("too_deep", "слишком глубоко"),
    ("live_socket", "живой сокет"),
    ("socket_node", "узел-сокет"),
    ("held", "держит процесс"),
)
# После rename меняется только то, что может появиться за секунды: процессы и сокеты.
RECHECK = ("live_socket", "socket_node", "held")
GONE = "запись исчезла"


def first_refusal(facts, which=None):
    for attr, reason in REFUSALS:
        if (which is None or attr in which) and getattr(facts, attr):
            return reason
    return None


# Всё, что не в реестре отходов, — чужие данные: удалить их можно только со словом владельца.
UNKNOWN = Decision("неизвестное", "без слова владельца не трогается", "оставить")


def is_waste(name, cfg) -> bool:
    return name.startswith(tuple(cfg["отходы_проб"]))


def decide(facts, cfg, mode, now) -> Decision:
    limit_h = cfg["сроки_ч"][f"отходы_{mode}"]
    if (now - facts.newest) / 3600 < limit_h:
        return Decision("отходы", f"моложе {limit_h} ч", "оставить")
    reason = first_refusal(facts)
    if reason:
        return Decision("отходы", reason, "отказ")
    if facts.data_like:
        return Decision("отходы", "похоже на данные", "карантин")
    return Decision("отходы", "отход пробы", "удалить")


# --- журнал ---------------------------------------------------------------

def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def journal_write(journal, row) -> bool:
    """False — строка не легла; вызывающий обязан НЕ удалять."""
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8", "surrogateescape")
    try:
        fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            return os.write(fd, line) == len(line)
        finally:
            os.close(fd)
    except OSError as exc:
        say(f"tmp: журнал удалений не пишется ({exc.strerror}) — запись не тронута")
        return False


def journal_row(path, facts, decision, now, action, **extra) -> dict:
    return {"когда": _stamp(), "путь": path, "мб": round(facts.size_unique_mb, 3),
            "возраст_ч": round((now - facts.newest) / 3600, 2), "правило": decision.rule,
            "класс": decision.klass, "действие": action, **extra}


# --- удаление ---------------------------------------------------------------

def remove_tree(dir_fd, node, root_dev, expected_uid, lstat, depth=0) -> int:
    """Возвращает число оставшихся узлов. chmod только на своих каталогах."""
    try:
        st = lstat(node, dir_fd=dir_fd)
    except FileNotFoundError:
        return 0
    if st.st_dev != root_dev:
        return 1
    if not stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(node, dir_fd=dir_fd)
            return 0
        except FileNotFoundError:
            return 0
        except OSError:
            return 1
    if depth >= MAX_DEPTH:
        return 1
    try:
        fd = os.open(node, DIR_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return 0
    except OSError:
        return 1
    left = 0
    try:
        if os.fstat(fd).st_dev != root_dev:
            return 1
        if (st.st_mode & 0o700) != 0o700 and st.st_uid == expected_uid:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) | 0o700)
        for child in os.listdir(fd):
            left += remove_tree(fd, child, root_dev, expected_uid, lstat, depth + 1)
    finally:
        os.close(fd)
    if left:
        return left + 1
    try:
        os.rmdir(node, dir_fd=dir_fd)
        return 0
    except FileNotFoundError:
        return 0
    except OSError:
        return 1


def _mkdir_at(root_fd, rel):
    try:
        os.mkdir(rel, 0o700, dir_fd=root_fd)
    except FileExistsError:
        pass


def _put_back(root_fd, rel, name) -> bool:
    try:
        os.rename(rel, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        return True
    except OSError as exc:
        say(f"tmp: обратный перенос {name} не удался ({exc.strerror}) — остаётся в карантине")
        return False


# --- ход ---------------------------------------------------------------

class Sweep:
    def __init__(self, root, mode, root_fd, *, expected_uid, get_holders, lstat, mounts,
                 journal, cfg, now):
        self.root, self.mode, self.root_fd = root, mode, root_fd
        self.uid, self.get_holders, self.lstat = expected_uid, get_holders, lstat
        self.mounts, self.journal, self.cfg, self.now = mounts, journal, cfg, now
        self.root_dev = os.fstat(root_fd).st_dev
        self.holders = None
        self.data_suffixes = tuple(cfg["похоже_на_данные"])

    def facts(self, dir_fd, node, path, skip_top_ctime=False) -> Facts:
        return entry_facts(dir_fd, node, root_dev=self.root_dev, expected_uid=self.uid,
                           holders=self.holders, lstat=self.lstat, entry_path=path,
                           mounts=self.mounts, data_suffixes=self.data_suffixes,
                           skip_top_ctime=skip_top_ctime)

    def row(self, path, facts, decision, action, **extra):
        return journal_row(path, facts, decision, self.now, action, **extra)

    def quarantine_classes(self) -> dict:
        """{хеш12: класс} по строкам «в карантин <хеш12>»: запись без строки не удаляется."""
        marker = "в карантин ".encode()
        named = {}
        try:
            with open(self.journal, "rb") as handle:
                for raw in handle:
                    if marker not in raw:
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8", "surrogateescape"))
                    except ValueError:
                        continue
                    action = str(row.get("действие", ""))
                    if action.startswith("в карантин "):
                        named[action.split()[-1]] = row.get("класс")
        except OSError:
            pass
        return named

    def clean_quarantine(self, report):
        try:
            q_fd = os.open(QUARANTINE, DIR_FLAGS, dir_fd=self.root_fd)
        except OSError:
            return
        named = self.quarantine_classes()
        try:
            for day in os.listdir(q_fd):
                try:
                    day_fd = os.open(day, DIR_FLAGS, dir_fd=q_fd)
                except OSError:
                    continue
                try:
                    for item in os.listdir(day_fd):
                        if item not in named:
                            report.nameless += 1
                        elif named[item] == "отходы":
                            self._expire(day_fd, f"{self.root}/{QUARANTINE}/{day}/{item}", item,
                                         report)
                finally:
                    os.close(day_fd)
        finally:
            os.close(q_fd)

    def _expire(self, day_fd, path, item, report):
        facts = self.facts(day_fd, item, path)
        if facts.vanished or (self.now - facts.newest) / 3600 < self.cfg["сроки_ч"]["карантин"]:
            return
        decision = Decision("карантин", "срок карантина", "удалить")
        reason = first_refusal(facts)
        if reason:
            journal_write(self.journal, self.row(path, facts, decision, f"отказ: {reason}"))
            return
        if not journal_write(self.journal, self.row(path, facts, decision, "удалено")):
            return
        left = remove_tree(day_fd, item, self.root_dev, self.uid, self.lstat)
        if not left:
            report.deleted += 1

    def judge_all(self, report, dry_run):
        protected = tuple(self.cfg["защищено"])
        chosen = []
        for name in sorted(os.listdir(self.root_fd)):
            if name.startswith(protected):
                continue
            if not is_waste(name, self.cfg):
                report.decisions[name] = UNKNOWN
                continue
            path = f"{self.root}/{name}"
            facts = self.facts(self.root_fd, name, path)
            if facts.vanished:
                continue
            decision = decide(facts, self.cfg, self.mode, self.now)
            report.decisions[name] = decision
            if decision.action == "отказ":
                report.refused += 1
                if not dry_run:
                    journal_write(self.journal, self.row(path, facts, decision,
                                                         f"отказ: {decision.rule}"))
            elif decision.action in ("удалить", "карантин") and not dry_run:
                chosen.append((name, facts, decision))
        return chosen

    def move_to_quarantine(self, chosen):
        """Строка журнала ложится ДО rename: убитый ход оставляет имя, а не безымянную запись."""
        day = time.strftime("%Y%m%d", time.localtime(self.now))
        _mkdir_at(self.root_fd, QUARANTINE)
        _mkdir_at(self.root_fd, f"{QUARANTINE}/{day}")
        moved = []
        for name, facts, decision in chosen:
            path = f"{self.root}/{name}"
            seed = f"{path}\0{self.now}\0{time.time_ns()}"
            hash12 = hashlib.sha256(seed.encode("utf-8", "surrogateescape")).hexdigest()[:12]
            rel = f"{QUARANTINE}/{day}/{hash12}"
            intent = self.row(path, facts, decision, f"в карантин {hash12}",
                              куда=f"{self.root}/{rel}", было_имя=name)
            if not journal_write(self.journal, intent):
                continue
            try:
                os.rename(name, rel, src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)
            except OSError:
                journal_write(self.journal, self.row(path, facts, decision,
                                                     "отказ: перенос не удался"))
                continue
            moved.append((name, rel, facts, decision))
        return moved

    def recheck(self, rel, decision, again):
        """Причина вернуть запись или None. Держатель мог появиться между судом и rename."""
        if isinstance(again, NoRight):
            return "нет права видеть чужие процессы"
        self.holders = again
        fresh = self.facts(self.root_fd, rel, f"{self.root}/{rel}", skip_top_ctime=True)
        if fresh.vanished:
            return GONE
        limit_h = self.cfg["сроки_ч"][f"{decision.klass}_{self.mode}"]
        if (self.now - fresh.newest) / 3600 < limit_h:
            return "посвежела"
        return first_refusal(fresh, RECHECK)

    def finish(self, moved, again, report):
        for name, rel, facts, decision in moved:
            path = f"{self.root}/{name}"
            reason = self.recheck(rel, decision, again)
            if reason == GONE:
                journal_write(self.journal, self.row(path, facts, decision,
                                                     "исчезла после переноса"))
                continue
            if reason:
                if _put_back(self.root_fd, rel, name):
                    journal_write(self.journal, self.row(path, facts, decision, "возвращено",
                                                         причина=reason))
                continue
            if decision.action == "карантин":
                report.quarantined += 1
                continue
            if not journal_write(self.journal, self.row(path, facts, decision, "удалено")):
                _put_back(self.root_fd, rel, name)
                continue
            left = remove_tree(self.root_fd, rel, self.root_dev, self.uid, self.lstat)
            if left:
                journal_write(self.journal, self.row(path, facts, decision,
                                                     f"осталось узлов {left}",
                                                     куда=f"{self.root}/{rel}"))
            else:
                report.deleted += 1


def sweep(root, mode, *, expected_uid, holders, geteuid=os.geteuid, lstat=os.lstat,
          mountinfo=("/proc/self/mountinfo",), after_rename=None, journal, cfg, now,
          dry_run=False) -> Report:
    report = Report()
    if geteuid() == 0:
        # Под root чужие права не ограничивают rename/unlink — защита хозяином теряет смысл.
        say("tmp: отказ — уборщик под root")
        report.skipped = "уборщик под root"
        return report
    get_holders = holders if callable(holders) else (lambda: holders)
    found = get_holders()
    if isinstance(found, NoRight):
        say("tmp: нет права видеть чужие процессы — шаг пропущен")
        report.skipped = found.reason
        return report
    root_fd = os.open(root, DIR_FLAGS)
    try:
        run = Sweep(root, mode, root_fd, expected_uid=expected_uid, get_holders=get_holders,
                    lstat=lstat, mounts=read_mounts(mountinfo), journal=journal, cfg=cfg,
                    now=now)
        run.holders = found
        if mode == "ночь" and not dry_run:
            run.clean_quarantine(report)
        chosen = run.judge_all(report, dry_run)
        if chosen:
            moved = run.move_to_quarantine(chosen)
            if after_rename and moved:
                after_rename([f"{root}/{rel}" for _, rel, _, _ in moved])
            run.finish(moved, get_holders(), report)
        return report
    finally:
        os.close(root_fd)
        summary(report)


def summary(report: Report) -> None:
    counts: dict = {}
    for decision in report.decisions.values():
        key = (decision.klass, decision.action)
        counts[key] = counts.get(key, 0) + 1
    for (klass, action), count in sorted(counts.items()):
        say(f"{klass}\t{count}\t-\t{action}")
    say(f"tmp: удалено {report.deleted}, в карантин {report.quarantined}, "
        f"отказов {report.refused}, без строки журнала: {report.nameless}")


def restore(hash12, journal):
    wanted = f"в карантин {hash12}"
    hit = None
    try:
        with open(journal, "rb") as handle:
            for raw in handle:
                if wanted.encode() in raw:
                    row = json.loads(raw.decode("utf-8", "surrogateescape"))
                    hit = row if row.get("действие") == wanted else hit
    except OSError:
        return "отказ: журнал не читается"
    if hit is None:
        return "отказ: нет такой записи в журнале"
    src, dst = hit["куда"], hit["путь"]
    if os.path.lexists(dst):
        return "отказ: прежнее имя занято"
    if not os.path.lexists(src):
        return "отказ: в карантине нет"
    try:
        os.rename(src, dst)
    except OSError as exc:
        return f"отказ: перенос не удался ({exc.strerror})"
    journal_write(journal, {**hit, "когда": _stamp(), "действие": "возвращено"})
    return None


# --- корень и CLI ---------------------------------------------------------------

def check_root(path, *, stand):
    if not path or not os.path.isabs(path):
        return "корень не абсолютный"
    try:
        st = os.lstat(path)
    except OSError as exc:
        return f"корень не читается: {exc.strerror}"
    if stat.S_ISLNK(st.st_mode):
        return "корень — симлинк"
    if not stat.S_ISDIR(st.st_mode):
        return "корень не каталог"
    real = os.path.realpath(path)
    if stand:
        if real != os.path.normpath(path):
            return "realpath стенда не совпал"
        return None if real.startswith(ROOT + "/") else "стенд вне /tmp"
    if real != ROOT:
        return "корень не /tmp"
    if st.st_uid != 0:
        return "хозяин /tmp не root"
    if stat.S_IMODE(st.st_mode) != 0o1777:
        return "режим /tmp не 1777"
    return None


def load_cfg():
    import yaml
    for candidate in (HERE.parent / "config" / "tmp-sweep.yaml",
                      HERE.parent / "harness" / "config" / "tmp-sweep.yaml"):
        if candidate.is_file():
            return yaml.safe_load(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError("нет tmp-sweep.yaml")


def sweep_enabled() -> bool:
    """Выключатель судит сама дверь удаления, а не звонящий: окружение старше файла, пусто = не задано."""
    sys.path.insert(0, str(HERE))
    from lib import config
    value = os.environ.get("TMP_SWEEP_ENABLED") or config.из_файла(
        config.путь_harness_conf()).get("TMP_SWEEP_ENABLED", "")
    return value == "1"


def log_dir() -> str:
    if os.environ.get("LOG_DIR"):
        return os.environ["LOG_DIR"]
    sys.path.insert(0, str(HERE))
    from lib import config
    return config.log_dir()


def selftest_full() -> int:
    """Полный ход на стенде с 20 отходами по 1 МБ; стенд за собой убирает."""
    import shutil
    stand = Path(tempfile.mkdtemp(prefix="уборка-стенд-", dir=ROOT))
    try:
        root = stand / "корень"
        root.mkdir()
        for i in range(20):
            waste = root / f"обновление-стенд-{i:02d}"
            waste.mkdir()
            (waste / "d.bin").write_bytes(os.urandom(1 << 20))
        journal = stand / "журнал.jsonl"
        sweep(str(root), "ночь", expected_uid=os.getuid(), holders=read_holders,
              journal=str(journal), cfg=load_cfg(), now=time.time() + 7 * 86400)
        deleted = sum(1 for raw in journal.read_bytes().split(b"\n")
                      if raw.strip() and json.loads(raw).get("действие") == "удалено")
    finally:
        shutil.rmtree(stand)
    note = os.environ.get("TMP_SWEEP_SELFTEST_NOTE")
    if note:
        Path(note).write_text(f"{stand}\nудалено {deleted}\n", encoding="utf-8")
    say(f"selftest-full: удалено {deleted}")
    return 0 if deleted == 20 else 1


def verify_journal(journal) -> int:
    bad = 0
    with open(journal, "rb") as handle:
        for raw in handle:
            if raw.strip():
                row = json.loads(raw.decode("utf-8", "surrogateescape"))
                if not os.path.realpath(row.get("путь", "")).startswith(ROOT + "/"):
                    bad += 1
    say(f"нарушений {bad}")
    return 1 if bad else 0


def free_bytes(path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def take_lock(logs):
    os.makedirs(f"{logs}/locks/services", exist_ok=True)
    fd = os.open(f"{logs}/locks/services/tmp-sweep.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


KNOWN = {"--держатели", "--selftest", "--selftest-full", "--ночь", "--давление", "--dry-run",
         "--вернуть", "--сверить-журнал"}


def main(argv) -> int:
    flags = [a for a in argv if a.startswith("--")]
    if not flags or any(f not in KNOWN for f in flags):
        say("tmp: незнакомый ключ")
        return 2
    if "--selftest" in flags:
        os.execv(sys.executable, [sys.executable, str(HERE / "test_tmp_sweep.py")])
    if "--держатели" in flags:
        print(json.dumps(collect_holders(), ensure_ascii=False))
        return 0
    if "--selftest-full" in flags:
        return selftest_full()
    # Подмена корня разрешена только стенду пробы, который зовёт sweep() напрямую.
    if "TMP_SWEEP_ROOT" in os.environ:
        say("tmp: отказ — подмена корня вне стенда")
        return 2
    destructive = ("--ночь" in flags or "--давление" in flags) and "--dry-run" not in flags
    if destructive and not sweep_enabled():
        say("tmp: отказ — TMP_SWEEP_ENABLED не 1, уборка /tmp выключена")
        return 2
    logs = log_dir()
    journal = f"{logs}/уборка-tmp.jsonl"
    if "--сверить-журнал" in flags:
        return verify_journal(journal)
    if "--вернуть" in flags and argv.index("--вернуть") + 1 >= len(argv):
        say("tmp: --вернуть без хеша")
        return 2
    if "--ночь" not in flags and "--давление" not in flags and "--вернуть" not in flags:
        say("tmp: нужен режим --ночь или --давление")
        return 2
    lock_fd = take_lock(logs)
    if lock_fd is None:
        say("tmp: лок tmp-sweep занят — другой ход идёт")
        return 0
    if "--вернуть" in flags:
        say(restore(argv[argv.index("--вернуть") + 1], journal) or "возвращено")
        return 0
    reason = check_root(ROOT, stand=False)
    if reason:
        say(f"tmp: отказ — {reason}")
        return 2
    free_before = free_bytes(ROOT)
    sweep(ROOT, "ночь" if "--ночь" in flags else "давление", expected_uid=os.getuid(),
          holders=read_holders, journal=journal, cfg=load_cfg(), now=time.time(),
          dry_run="--dry-run" in flags)
    say(f"10. /tmp: освобождено {max(0, free_bytes(ROOT) - free_before) // 1048576} МБ")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
