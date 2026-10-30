#!/usr/bin/env python3
"""Проба держателя замков раскладки (lib/layout_lock.py): update.lock и layout.lock.

Модуль берётся РЯДОМ с пробой (Path(__file__).parent / "lib"): мутации подставляет
mutation-probe.sh копией корня scripts/, и мутированный модуль обязан дойти до пробы.

Договор модуля (план Ф-в Task 3, контракт pretask-fv.yaml interfaces.lock_module, решение ж):
  lock_key(path) -> "%02x:%02x:%d" (major, minor устройства, inode) — как ключ в /proc/locks;
  holder_pids(locks_text, key) -> [pid] — строки FLOCK-держателей, ключ целым полем;
  ancestors(pid, read_status) -> [pid] — цепочка PPid; цикл, нет файла, PPid 0 — конец;
  judge(lock_path, layout_pid, locks_text=None, read_status=None) -> (code, text) — update.lock
      читается БЕЗ создания (нет файла — 0; есть — O_RDONLY и flock LOCK_NB);
  fd_holders(lock_path) -> [pid] — процессы с открытым дескриптором на файл замка (решение ж:
      layout.lock держит bash раскладчика через flock(1), и /proc/locks называет pid
      ВЫШЕДШЕГО flock(1), а после SIGKILL bash — живого наследника не называет вовсе);
  CLI: --update-lock <путь> --layout-pid <pid> → 0 | 3 (stderr «РАСКЛАДКА НЕ НАЧАТА: …»);
       --who-holds <путь> → stdout «держатель <pid>[, <pid>]» | «держатель не найден: <причина>», 0.

Каждый случай — свой mktemp; живой /var/log/harness/locks и живой update.lock не открываются:
дети пробы получают LOG_DIR и HARNESS_INSTALL_CONF стенда. Модуля нет — каждый случай
красный «нет модуля», без трассы. Итог: «ЗАМОК РАСКЛАДКИ: путей N, неудач M», код 0/1.
"""
import fcntl
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
MODULE_PATH = (HERE / "lib" / "layout_lock.py").absolute()
READY_LIMIT_SEC = 10
REFUSAL = "РАСКЛАДКА НЕ НАЧАТА: "
BUSY_UPDATE = "РАСКЛАДКА НЕ НАЧАТА: идёт обновление харнеса — раскладка подождёт"

# Держатель-ребёнок пробы. Готовность — ЕГО слово (метка с pid), а не sleep пробы.
# mode «own»: сам берёт flock, как update.py:326; mode «inherited»: держит дескриптор,
# унаследованный от bash (как генератор раскладчика), и ничего не открывает сам.
HOLDER_SOURCE = r'''
import fcntl, os, sys, time
mode, path, mark, release = sys.argv[1:5]
if mode == "own":
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
with open(mark + ".tmp", "w", encoding="utf-8") as handle:
    handle.write(f"держу {os.getpid()}\n")
os.rename(mark + ".tmp", mark)
deadline = time.monotonic() + 20
while not os.path.exists(release) and time.monotonic() < deadline:
    time.sleep(0.05)
'''


def load_module():
    if not MODULE_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("layout_lock", MODULE_PATH)
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

    def skipped(self, label: str, name: str, why: str) -> None:
        print(f"  ПРОПУЩЕН {label} {name}: {why}")

    def crashed(self, label: str, error: Exception | None) -> None:
        self.paths += 1
        self.failures += 1
        if LL is None:
            print(f"  ПЛОХО {label} нет модуля {MODULE_PATH}")
        else:
            print(f"  ПЛОХО {label} проба упала: {type(error).__name__}: {error}")


PROBE = Probe()
LL = None


# ─────────────────────────────── стенд ───────────────────────────────────────

class Stand:
    """mktemp со своим LOG_DIR; дети пробы видят только его."""

    def __init__(self, base: str | None = None) -> None:
        self.t = Path(tempfile.mkdtemp(prefix="layout-lock-probe-", dir=base))
        self.log_dir = self.t / "log"
        (self.log_dir / "locks").mkdir(parents=True)
        self.holder_script = self.t / "holder.py"
        self.holder_script.write_text(HOLDER_SOURCE, encoding="utf-8")
        self.children: list = []

    def __enter__(self) -> "Stand":
        return self

    def __exit__(self, *_exc) -> None:
        (self.t / "отпусти").touch()
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
        for path in self.t.rglob("*"):
            if not path.is_symlink():
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.t, ignore_errors=True)

    @property
    def update_lock(self) -> Path:
        return self.log_dir / "locks" / "update.lock"

    def env(self) -> dict:
        env = dict(os.environ)
        env["LOG_DIR"] = str(self.log_dir)
        env["HARNESS_INSTALL_CONF"] = str(self.t / "install.conf")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def entries(self) -> set:
        return {str(p.relative_to(self.t)) for p in self.t.rglob("*")}

    def start_holder(self, path: Path, tag: str) -> tuple:
        """Держатель-ребёнок берёт flock сам; возвращает (Popen, pid из его метки)."""
        mark = self.t / f"метка-{tag}"
        child = subprocess.Popen([sys.executable, str(self.holder_script), "own", str(path),
                                  str(mark), str(self.t / "отпусти")], env=self.env())
        self.children.append(child)
        return child, wait_mark(mark, child)

    def release(self, child) -> bool:
        (self.t / "отпусти").touch()
        try:
            child.wait(timeout=READY_LIMIT_SEC)
        except subprocess.TimeoutExpired:
            return False
        (self.t / "отпусти").unlink()
        return True

    def cli(self, *args: str, via_bash_pid: bool = False) -> subprocess.CompletedProcess:
        """CLI модуля дочерним процессом. via_bash_pid: --layout-pid — pid обёртки bash."""
        if via_bash_pid:
            command = ["bash", "-c", 'python3 "$1" --update-lock "$2" --layout-pid $$; exit $?',
                       "_", str(MODULE_PATH), args[0]]
        else:
            command = [sys.executable, str(MODULE_PATH), *args]
        return subprocess.run(command, env=self.env(), capture_output=True, text=True, timeout=30)


def wait_mark(mark: Path, child) -> int | None:
    deadline = time.monotonic() + READY_LIMIT_SEC
    while time.monotonic() < deadline:
        if mark.is_file():
            found = re.fullmatch(r"держу (\d+)\n", mark.read_text(encoding="utf-8"))
            return int(found.group(1)) if found else None
        if child.poll() is not None:
            return None
        time.sleep(0.02)
    return None


def flock_free(path: Path) -> bool:
    """`flock -n <файл> true` — rc 0. Файл обязан существовать (flock(1) создал бы его)."""
    return subprocess.run(["flock", "-n", str(path), "true"]).returncode == 0


def proc_locks() -> str:
    return Path("/proc/locks").read_text(encoding="utf-8")


def real_status_text() -> str:
    """Живой /proc/self/status — образец формата для поддельных (чужой вывод снимаем, не пишем)."""
    return Path("/proc/self/status").read_text(encoding="utf-8")


def fake_status(ppid: int) -> str:
    return re.sub(r"^PPid:\t\d+$", f"PPid:\t{ppid}", real_status_text(), flags=re.M)


def named_pids(text: str) -> set:
    found = re.search(r"держатель ([\d, ]+)", text)
    return {int(x) for x in re.findall(r"\d+", found.group(1))} if found else set()


# ─────────────────────────────── случаи ──────────────────────────────────────

def case_l1() -> None:
    """holder_pids: держатели FLOCK по ключу ЦЕЛЫМ полем; ждущий «->» и POSIX — не держатели.
    Строки сняты с живого /proc/locks (формат ядра 6.8, два пробела между полями).
    Зелен на поломке? «ключ подстрокой» — нет: ключ 08:01:77 и строка 08:01:7770 обязаны дать []."""
    text = ("1: FLOCK  ADVISORY  WRITE 4242 08:01:777 0 EOF\n"
            "1: -> FLOCK  ADVISORY  WRITE 5151 08:01:777 0 EOF\n"
            "2: POSIX  ADVISORY  WRITE 99 08:01:778 0 EOF\n"
            "3: POSIX  ADVISORY  WRITE 98 08:01:777 0 EOF\n"
            "4: FLOCK  ADVISORY  WRITE 3131 08:01:7770 0 EOF\n"
            "5: FLOCK  ADVISORY  WRITE 2121 108:01:777 0 EOF\n")
    got = LL.holder_pids(text, "08:01:777")
    PROBE.check("L1", "ключ 08:01:777 → [4242] (без ждущего 5151, POSIX 98, 7770, 108:01)",
                list(got) == [4242], f"дал {got!r}")
    got = LL.holder_pids(text, "08:01:77")
    PROBE.check("L1", "ключ 08:01:77 → [] (не подстрока)", list(got) == [], f"дал {got!r}")
    got = LL.holder_pids("", "08:01:777")
    PROBE.check("L1", "пустой текст → []", list(got) == [], f"дал {got!r}")


def case_l2() -> None:
    """ancestors: цепочка PPid по поддельным status (формат — с живого /proc/self/status).
    Зелен на поломке? «цикл не обрывается» — нет: зависание судит предел времени, исключение — трасса метки."""
    table = {5: fake_status(4), 4: fake_status(1), 1: fake_status(0)}
    got = LL.ancestors(5, lambda pid: table[pid])
    PROBE.check("L2", "5 → 4 → 1 → [4, 1]", list(got) == [4, 1], f"дал {got!r}")

    def missing(pid: int) -> str:
        if pid == 4:
            raise FileNotFoundError(f"/proc/{pid}/status")
        return table[pid]
    got = LL.ancestors(5, missing)
    PROBE.check("L2", "нет /proc/4/status → [4], не исключение", list(got) == [4], f"дал {got!r}")
    cycle = {7: fake_status(8), 8: fake_status(7)}
    started = time.monotonic()
    got = LL.ancestors(7, lambda pid: cycle[pid])
    PROBE.check("L2", "цикл 7 ↔ 8 обрывается: 8 в цепочке, длина ≤ 2, быстро",
                8 in got and len(got) <= 2 and time.monotonic() - started < 2, f"дал {got!r}")


def case_l3() -> None:
    """update.lock нет → код 0, файл НЕ создан (пятого места записи нет, решение 5); каталог
    без права записи — тоже 0, не трасса; довод без значения — не 0.
    Зелен на поломке? «open(a+)/O_CREAT» — нет: после вызова файла быть не должно,
    а в каталоге 0555 создание упало бы."""
    with Stand() as s:
        before = s.entries()
        code, text = LL.judge(str(s.update_lock), os.getpid())
        PROBE.check("L3", "judge: нет файла → код 0", code == 0, f"код {code}, {text!r}")
        PROBE.check("L3", "judge: файл не создан", not os.path.lexists(s.update_lock),
                    "update.lock появился")
        PROBE.check("L3", "в стенде ничего нового", s.entries() == before,
                    f"новое {sorted(s.entries() - before)}")
        result = s.cli("--update-lock", str(s.update_lock), "--layout-pid", str(os.getpid()))
        PROBE.check("L3", "CLI: нет файла → код 0, файл не создан",
                    result.returncode == 0 and not os.path.lexists(s.update_lock),
                    f"код {result.returncode}; {result.stderr.strip()}")
        result = s.cli("--update-lock", str(s.update_lock))
        PROBE.check("L3", "CLI без --layout-pid → не 0 (ошибка вызова не пропускает раскладку)",
                    result.returncode != 0, f"код {result.returncode}")
        if os.geteuid() == 0:
            PROBE.skipped("L3", "каталог 0555", "под root права не держат")
            return
        os.chmod(s.log_dir / "locks", 0o555)
        try:
            code, text = LL.judge(str(s.update_lock), os.getpid())
        finally:
            os.chmod(s.log_dir / "locks", 0o755)
        PROBE.check("L3", "каталог 0555, файла нет → код 0, файл не создан",
                    code == 0 and not os.path.lexists(s.update_lock), f"код {code}, {text!r}")


def case_l4() -> None:
    """update.lock есть и свободен → 0, и проверка не оставила замок за собой; файл 0444
    (чужой, только чтение) — тоже 0: открытие на чтение, flock(2) берётся и так.
    Зелен на поломке? «замок не отпущен» — нет: flock -n сразу после — rc 0; «открытие на
    запись» — нет: 0444 даёт EACCES."""
    with Stand() as s:
        s.update_lock.write_bytes(b"")
        code, text = LL.judge(str(s.update_lock), os.getpid())
        PROBE.check("L4", "свободен → код 0", code == 0, f"код {code}, {text!r}")
        PROBE.check("L4", "сразу после: flock -n <файл> true → rc 0", flock_free(s.update_lock),
                    "замок остался взят")
        result = s.cli("--update-lock", str(s.update_lock), "--layout-pid", str(os.getpid()))
        PROBE.check("L4", "CLI: свободен → 0, flock -n после → rc 0",
                    result.returncode == 0 and flock_free(s.update_lock),
                    f"код {result.returncode}; {result.stderr.strip()}")
        PROBE.check("L4", "файл не тронут (размер 0)", s.update_lock.stat().st_size == 0,
                    f"размер {s.update_lock.stat().st_size}")
        if os.geteuid() == 0:
            PROBE.skipped("L4", "файл 0444", "под root права не держат")
            return
        os.chmod(s.update_lock, 0o444)
        code, text = LL.judge(str(s.update_lock), os.getpid())
        PROBE.check("L4", "файл 0444, свободен → код 0 (открытие на чтение)", code == 0,
                    f"код {code}, {text!r}")


def run_ancestor(s: Stand) -> tuple:
    """L5: проба держит update.lock сама (fcntl.flock, как update.py:326), CLI — её внук через bash."""
    fd = os.open(s.update_lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = s.cli(str(s.update_lock), via_bash_pid=True)
    finally:
        os.close(fd)
    output = result.stdout + result.stderr
    ok = result.returncode == 0 and f"предок {os.getpid()}" in output
    return ok, f"код {result.returncode}; {output.strip()!r}"


def case_l5() -> None:
    """Держатель — предок раскладки (update.py зовёт раскладчик) → проходит, слово «предок <pid>».
    Зелен на поломке? «отказ любому держателю» — нет: код обязан быть 0."""
    with Stand() as s:
        ok, detail = run_ancestor(s)
        PROBE.check("L5", "держит проба (предок CLI) → код 0, «замок обновления держит предок <pid>»",
                    ok, detail)


def run_stranger(s: Stand, tag: str) -> tuple:
    """L6: держит отдельный ребёнок пробы (брат CLI, не предок)."""
    child, pid = s.start_holder(s.update_lock, tag)
    if pid is None:
        return False, "стенд: держатель не сказал «держу» за 10 с"
    if flock_free(s.update_lock):
        return False, "стенд: держатель сказал «держу», а flock -n свободен"
    result = s.cli(str(s.update_lock), via_bash_pid=True)
    released = s.release(child)
    want = f"{BUSY_UPDATE} (держатель {pid})"
    ok = (result.returncode == 3 and result.stderr.startswith(REFUSAL + "идёт обновление харнеса")
          and want in result.stderr and released)
    return ok, f"код {result.returncode}; stderr {result.stderr.strip()!r}; ждали {want!r}; отпущен {released}"


def case_l6() -> None:
    """Держатель — НЕ предок (раскладка запущена рядом с обновлением) → код 3, pid держателя назван.
    Зелен на поломке? «пропуск любому держателю» — нет: код обязан быть 3."""
    with Stand() as s:
        ok, detail = run_stranger(s, "l6")
        PROBE.check("L6", "держит брат CLI → код 3, «…раскладка подождёт (держатель <pid>)»", ok, detail)


def case_l7() -> None:
    """Суд держателя по данным: не найден → 3 (не пропуск); предок или сама раскладка → 0;
    чужой → 3. Замок занят по-настоящему (проба держит), строки /proc/locks подставлены.
    Зелен на поломке? «не найден = проходи» — нет: код обязан быть 3 со словами «держатель не найден»."""
    with Stand() as s:
        fd = os.open(s.update_lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            key = LL.lock_key(str(s.update_lock))
            line = f"1: FLOCK  ADVISORY  WRITE 4242 {key} 0 EOF\n"
            chain = {10: fake_status(20), 20: fake_status(4242), 4242: fake_status(1),
                     30: fake_status(1), 1: fake_status(0)}

            def status(pid: int) -> str:
                return chain[pid]
            code, text = LL.judge(str(s.update_lock), 10, locks_text="", read_status=status)
            PROBE.check("L7", "занят, строки нет → код 3, «держатель не найден»",
                        code == 3 and "держатель не найден" in text and text.startswith(REFUSAL),
                        f"код {code}, {text!r}")
            code, text = LL.judge(str(s.update_lock), 10, locks_text=line, read_status=status)
            PROBE.check("L7", "держатель 4242 — дед раскладки 10 → код 0, «предок 4242»",
                        code == 0 and "предок 4242" in text, f"код {code}, {text!r}")
            code, text = LL.judge(str(s.update_lock), 4242, locks_text=line, read_status=status)
            PROBE.check("L7", "держатель — сама раскладка 4242 → код 0", code == 0, f"код {code}, {text!r}")
            code, text = LL.judge(str(s.update_lock), 30, locks_text=line, read_status=status)
            PROBE.check("L7", "держатель 4242 не в цепочке 30 → 1 → код 3, «(держатель 4242)»",
                        code == 3 and f"{BUSY_UPDATE} (держатель 4242)" in text, f"код {code}, {text!r}")
        finally:
            os.close(fd)


def case_l8() -> None:
    """Повтор L5 и L6 по 5 раз — одинаково (гонка готовности держателя, свежий стенд на каждый).
    Зелен на поломке? «готовность по sleep» — нет: держатель, не успевший взять замок, даёт код 0 в L6."""
    results = {"L5": [], "L6": []}
    for round_no in range(5):
        with Stand() as s:
            results["L5"].append(run_ancestor(s)[0])
        with Stand() as s:
            results["L6"].append(run_stranger(s, f"l8-{round_no}")[0])
    for label, oks in results.items():
        PROBE.check("L8", f"{label} × 5 — все «ок»", all(oks), f"итоги {oks}")


def case_l9() -> None:
    """Ключ замка не зависит от ФС: lock_key совпадает с полем живого /proc/locks на каждой
    доступной ФС из /tmp и /dev/shm — major/minor по два знака hex, а не сырой st_dev. Одна ФС
    на машине (tmpfs /tmp, btrfs, zfs) — слово стенда, а не провал (ревью кода Ф-в R1-02).
    Зелен на поломке? «%x сырого st_dev» — нет: на ext4 дал бы «801:…», на tmpfs — «1c:…»,
    то есть одно двоеточие против двух на любой одной ФС."""
    places = [d for d in (tempfile.gettempdir(), "/dev/shm") if os.path.isdir(d) and os.access(d, os.W_OK)]
    kinds = set()
    for place in places:
        with Stand(base=place) as s:
            path = s.t / "x.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                mine = [ln.split() for ln in proc_locks().splitlines()
                        if ln.split()[1:2] == ["FLOCK"] and ln.split()[4:5] == [str(os.getpid())]
                        and ln.split()[5].endswith(f":{os.stat(path).st_ino}")]
                live_key = mine[0][5] if len(mine) == 1 else None
                key = LL.lock_key(str(path))
                kinds.add(os.major(os.stat(path).st_dev))
                PROBE.check("L9", f"{place}: lock_key == поле /proc/locks ({live_key})",
                            live_key is not None and key == live_key, f"lock_key {key!r}, строки {mine}")
                PROBE.check("L9", f"{place}: holder_pids(живой /proc/locks, ключ) == [pid пробы]",
                            list(LL.holder_pids(proc_locks(), key)) == [os.getpid()],
                            f"дал {LL.holder_pids(proc_locks(), key)!r}")
            finally:
                os.close(fd)
    # The «%x of raw st_dev» breakage reddens the key check on ANY single filesystem (one colon
    # against two), so a machine with one major is a stand word, not a failure: on tmpfs /tmp,
    # btrfs or zfs a red here would roll every update back as «new red gates».
    PROBE.check("L9", "стенд: судилась хотя бы одна ФС", bool(kinds), f"места {places}")
    if len(kinds) < 2:
        print(f"  слово стенда L9: различение major не судимо на этой машине: одна ФС "
              f"(major {sorted(kinds)} в {places})")


def case_l10() -> None:
    """Решение ж: зависший генератор держит layout.lock наследованным дескриптором; bash раскладчика
    убит SIGKILL (как таймаут update.py). --who-holds называет pid ЖИВОГО наследника — не pid
    вышедшего flock(1), который один и стоит в /proc/locks.
    Зелен на поломке? «держатель из /proc/locks» — нет: там pid вышедшего flock(1), стенд это проверяет."""
    with Stand() as s:
        lock = s.log_dir / "locks" / "layout.lock"
        mark = s.t / "метка-генератор"
        script = ('exec {fd}>>"$1" || exit 9; flock -w 0 "$fd" || exit 8; '
                  'python3 "$2" inherited "$1" "$3" "$4" & wait')
        bash = subprocess.Popen(["bash", "-c", script, "_", str(lock), str(s.holder_script),
                                 str(mark), str(s.t / "отпусти")], env=s.env())
        s.children.append(bash)
        generator_pid = wait_mark(mark, bash)
        if generator_pid is None:
            PROBE.check("L10", "стенд: генератор сказал «держу»", False, "метки нет за 10 с")
            return
        os.kill(bash.pid, signal.SIGKILL)
        bash.wait(timeout=READY_LIMIT_SEC)
        key = LL.lock_key(str(lock))
        listed = [int(ln.split()[4]) for ln in proc_locks().splitlines()
                  if ln.split()[1:2] == ["FLOCK"] and ln.split()[5:6] == [key]]
        PROBE.check("L10", "стенд: bash убит, замок держит наследник (flock -n не удаётся)",
                    not flock_free(lock), "замок свободен — стенд не воспроизвёл наследование")
        PROBE.check("L10", "стенд: /proc/locks НЕ называет генератор (случай различает способы)",
                    generator_pid not in listed, f"в /proc/locks {listed}, генератор {generator_pid}")
        result = s.cli("--who-holds", str(lock))
        named = named_pids(result.stdout)
        PROBE.check("L10", "--who-holds → код 0, «держатель <pid генератора>» и только он",
                    result.returncode == 0 and named == {generator_pid},
                    f"код {result.returncode}; stdout {result.stdout.strip()!r}; генератор {generator_pid}")
        (s.t / "отпусти").touch()
        deadline = time.monotonic() + READY_LIMIT_SEC
        while time.monotonic() < deadline and os.path.exists(f"/proc/{generator_pid}"):
            time.sleep(0.05)
        PROBE.check("L10", "генератор вышел → flock -n удаётся", flock_free(lock),
                    f"замок занят; генератор жив: {os.path.exists(f'/proc/{generator_pid}')}")
        result = s.cli("--who-holds", str(lock))
        PROBE.check("L10", "свободен → «держатель не найден: …», код 0",
                    result.returncode == 0 and "держатель не найден" in result.stdout
                    and not named_pids(result.stdout), f"код {result.returncode}; {result.stdout.strip()!r}")
        absent = s.log_dir / "locks" / "нет.lock"
        result = s.cli("--who-holds", str(absent))
        PROBE.check("L10", "файла нет → «держатель не найден», файл не создан",
                    "держатель не найден" in result.stdout and not os.path.lexists(absent),
                    f"код {result.returncode}; {result.stdout.strip()!r}")


CASES = (case_l1, case_l2, case_l3, case_l4, case_l5, case_l6, case_l7, case_l8, case_l9, case_l10)


def main() -> int:
    global LL
    try:
        LL = load_module()
    except Exception as error:      # a broken module must show as red per case, not a trace
        print(f"модуль {MODULE_PATH} не загрузился: {type(error).__name__}: {error}")
        LL = None
    print(f"модуль: {MODULE_PATH}" + ("" if LL is not None else " — НЕТ, каждый случай красный"))
    for case in CASES:
        label = case.__name__.removeprefix("case_").upper()
        if LL is None:
            PROBE.crashed(label, None)
            continue
        try:
            case()
        except Exception as error:      # a probe bug must show as red, not as a trace
            PROBE.crashed(label, error)
    print(f"ЗАМОК РАСКЛАДКИ: путей {PROBE.paths}, неудач {PROBE.failures}")
    return 0 if PROBE.failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
