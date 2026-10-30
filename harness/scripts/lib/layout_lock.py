"""Замки раскладки: судит, кто держит update.lock и layout.lock.

update.lock берёт update.py (fcntl.flock) на всё время постановки пакета и сам зовёт
раскладчик — поэтому держатель update.lock законно оказывается ПРЕДКОМ раскладки.
Переменной «меня зовёт обновление» нынешний update.py не ставит, и верить ей нельзя:
её мог оставить кто угодно. Держатель — факт ядра (/proc/locks), его и судим.

layout.lock держит bash раскладчика через flock(1) на унаследованном дескрипторе.
/proc/locks называет pid вышедшего flock(1), а после SIGKILL bash живого наследника
(генератор) не называет вовсе: настоящего держателя видно только в /proc/*/fd.

CLI:
    --update-lock <путь> --layout-pid <pid>   код 0 | 3 (отказ — stderr «РАСКЛАДКА НЕ НАЧАТА: …»)
    --who-holds <путь>                        «держатель <pid>[, <pid>]» | «держатель не найден: …», код 0
"""
import argparse
import fcntl
import os
import re
import sys

REFUSAL = "РАСКЛАДКА НЕ НАЧАТА: "
_PPID = re.compile(r"^PPid:\s*(\d+)\s*$", re.M)


def lock_key(path: str) -> str:
    """Ключ строки /proc/locks: major и minor по два знака hex, inode десятичный.
    Сырой st_dev здесь не годится: ядро печатает пару, а не слитое число."""
    info = os.stat(path)
    return "%02x:%02x:%d" % (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)


def holder_pids(locks_text: str, key: str) -> list:
    """Держатели FLOCK по ключу, поле целиком. Ждущие («->») замка не держат."""
    pids = []
    for line in locks_text.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK":
            continue
        if fields[5] == key and fields[4].isdigit():
            pids.append(int(fields[4]))
    return pids


def _read_status(pid: int) -> str:
    with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
        return handle.read()


def ancestors(pid: int, read_status=_read_status) -> list:
    """Цепочка родителей от ближайшего. Цикл, исчезнувший процесс, PPid 0 — конец цепочки."""
    chain = []
    seen = {pid}
    current = pid
    while True:
        try:
            found = _PPID.search(read_status(current))
        except OSError:
            return chain
        if not found:
            return chain
        parent = int(found.group(1))
        if parent <= 0 or parent in seen:
            return chain
        chain.append(parent)
        seen.add(parent)
        current = parent


def _read_locks() -> str:
    with open("/proc/locks", encoding="utf-8") as handle:
        return handle.read()


def _refuse(why: str) -> tuple:
    return 3, f"{REFUSAL}идёт обновление харнеса — раскладка подождёт ({why})"


def judge(lock_path: str, layout_pid: int, locks_text: str | None = None,
          read_status=_read_status) -> tuple:
    """(код, текст). Файл замка только читается: создание здесь было бы ещё одним
    местом записи в каталог замков и ронялось бы там, где права на запись нет."""
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return 0, ""
    except OSError as error:
        return 3, f"{REFUSAL}замок обновления не открыт: {lock_path}: {error.strerror}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _judge_busy(lock_path, layout_pid, locks_text, read_status)
        except OSError as error:
            return 3, f"{REFUSAL}замок обновления не проверен: {lock_path}: {error.strerror}"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 0, ""
    finally:
        os.close(fd)


def _judge_busy(lock_path: str, layout_pid: int, locks_text: str | None, read_status) -> tuple:
    key = lock_key(lock_path)
    if locks_text is None:
        try:
            locks_text = _read_locks()
        except OSError as error:
            return _refuse(f"держатель не найден: /proc/locks не прочитан: {error.strerror}")
    holders = holder_pids(locks_text, key)
    if not holders:
        return _refuse(f"держатель не найден: в /proc/locks нет строки FLOCK с ключом {key}")
    family = set(ancestors(layout_pid, read_status)) | {layout_pid}
    for pid in holders:
        if pid in family:
            return 0, f"замок обновления держит предок {pid} — раскладка идёт"
    return _refuse("держатель " + ", ".join(str(pid) for pid in holders))


def _scan_fds(path: str) -> tuple:
    """(pids, недоступно процессов): у кого открыт дескриптор на файл. Сравнение по
    (устройство, inode): ссылка в /proc/<pid>/fd для удалённого файла имени не даёт."""
    try:
        target = os.stat(path)
    except OSError:
        return [], 0
    wanted = (target.st_dev, target.st_ino)
    found, hidden = [], 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fd_dir = f"/proc/{name}/fd"
        try:
            entries = os.listdir(fd_dir)
        except PermissionError:
            hidden += 1
            continue
        except OSError:
            continue
        for entry in entries:
            try:
                info = os.stat(f"{fd_dir}/{entry}")
            except OSError:
                continue
            if (info.st_dev, info.st_ino) == wanted:
                found.append(int(name))
                break
    return sorted(found), hidden


def fd_holders(path: str) -> list:
    return _scan_fds(path)[0]


def who_holds(path: str) -> str:
    if not os.path.lexists(path):
        return f"держатель не найден: файла нет: {path}"
    pids, hidden = _scan_fds(path)
    if pids:
        return "держатель " + ", ".join(str(pid) for pid in pids)
    tail = f" (процессов без доступа к дескрипторам: {hidden})" if hidden else ""
    return f"держатель не найден: ни у одного процесса нет открытого дескриптора{tail}"


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description="замки раскладки")
    parser.add_argument("--update-lock")
    parser.add_argument("--layout-pid", type=int)
    parser.add_argument("--who-holds")
    args = parser.parse_args(argv)
    if args.who_holds is not None:
        print(who_holds(args.who_holds))
        return 0
    if args.update_lock is None or args.layout_pid is None:
        parser.error("нужны --update-lock и --layout-pid (или --who-holds)")
    code, text = judge(args.update_lock, args.layout_pid)
    if text:
        print(text, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
