#!/usr/bin/env python3
"""Проверка «поднялся» после перезагрузки в окне ротации и проверка «машина занята».

Карточка hr.reboot-in-rotation-window, спека docs/specs/2026-11-02-перезагрузка-в-окне-ротации.md
(разделы 3.1, 6.1, 6.2). Код знает только ВИДЫ проверок; ЧТО проверять — данные
harness/config/boot-check.yaml (секции «проверки» и «занято»).

    python3 scripts/boot_check.py [--метка <файл>] [--поздно] [--перечень <файл>]
    python3 scripts/boot_check.py --занято [--перечень <файл>]

Коды: 0 — красных нет; 1 — есть красные или «не состоялась»; 2 — перечень сломан;
5 — «рано», ничего не сделано. Ничего не поднимает и не гасит: юниты — только is-active.
Двери проб: HARNESS_BOOT_CHECK_LIST, HARNESS_BOOT_CHECK_TRANSCRIPTS, HARNESS_BOOT_CHECK_BOOT_TIME.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import config  # noqa: E402 — один читатель паспорта установки на весь харнес

KINDS = ("юнит", "tmux", "docker", "http", "команда", "ядро", "ответ_сессии")
BUSY_KINDS = ("замок", "процесс", "процесс_по_строке")
STEP_SEC = 10          # предел одной короткой проверки (юнит, tmux, docker, http, ядро)
COMMAND_LIMIT_SEC = 60
ANSWER_POLL_SEC = 5
ANSWER_WAIT_DEFAULT_SEC = 300
LATE_MIN_DEFAULT = 15
SAFETY_MARGIN_SEC = 30
POLICIES_UP = ("always", "unless-stopped")


class BrokenList(Exception):
    """Перечень нельзя исполнять: отказ, а не «всё зелёное»."""


def setting(key: str) -> str:
    """Окружение старше файлов; затем harness.conf, затем паспорт установки."""
    if os.environ.get(key):
        return os.environ[key]
    return config.из_файла(config.путь_harness_conf()).get(key) or config.конфиг().get(key, "")


def default_list() -> Path:
    if os.environ.get("HARNESS_BOOT_CHECK_LIST"):
        return Path(os.environ["HARNESS_BOOT_CHECK_LIST"])
    for candidate in (HERE.parent / "harness" / "config" / "boot-check.yaml",
                      HERE.parent / "config" / "boot-check.yaml"):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / "config" / "boot-check.yaml"


def load_list(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise BrokenList(f"перечень сломан: {path} не прочитан ({type(error).__name__})")
    if not isinstance(data, dict):
        raise BrokenList(f"перечень сломан: {path} — не словарь")
    return data


def entries_of(data: dict, section: str, kinds: tuple) -> list:
    entries = data.get(section)
    if not isinstance(entries, list) or not entries:
        raise BrokenList(f"перечень сломан: секция «{section}» пуста или её нет")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("имя"):
            raise BrokenList(f"перечень сломан: запись без имени в «{section}»: {str(entry)[:60]}")
        if entry.get("вид") not in kinds:
            raise BrokenList(f"перечень сломан: неизвестный вид «{entry.get('вид')}» у «{entry['имя']}»")
    return entries


def budget_sec(entries: list) -> int:
    total = 0
    for entry in entries:
        if entry["вид"] == "ответ_сессии":
            total += int(entry.get("ждать_сек") or ANSWER_WAIT_DEFAULT_SEC)
        elif entry["вид"] == "команда":
            total += int(entry.get("предел_сек") or COMMAND_LIMIT_SEC)
        else:
            total += STEP_SEC
    return total


def check_budget(entries: list) -> None:
    """Обёртка убила бы именно красный случай (смена молчит) кодом 124 до записи итога."""
    limit = setting("HARNESS_LIVE_CHECK_TIMEOUT") or setting("REBOOT_CHECK_TIMEOUT_SEC")
    if limit.isdigit() and budget_sec(entries) > int(limit) - SAFETY_MARGIN_SEC:
        raise BrokenList(f"перечень сломан: сумма пределов {budget_sec(entries)} с "
                         f"больше {limit} − {SAFETY_MARGIN_SEC} с")


def run(argv: list, timeout: int, cwd: str | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        return 127, f"нет команды {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: нет ответа за {timeout} с"
    return done.returncode, (done.stdout + done.stderr).strip()


# ── результат проверки: (состояние, подробность); состояния ok | red | skip ──

def check_unit(entry, mark):
    code, out = run(["systemctl", "is-active", entry["юнит"]], STEP_SEC)
    word = out.splitlines()[-1] if out else f"код {code}"
    return ("ok", "active") if word == "active" else ("red", word)


def check_tmux(entry, mark):
    session = entry.get("сессия") or setting("TMUX_SESSION")
    if not session:
        return "skip", "имя сессии не задано (TMUX_SESSION пуст)"
    # «=» — точное имя: без него tmux принимает сессию с тем же началом (B-17)
    code, _ = run(["tmux", "has-session", "-t", f"={session}"], STEP_SEC)
    return ("ok", f"сессия {session} есть") if code == 0 else ("red", f"сессии {session} нет")


def check_docker(entry, mark):
    code, out = run(["docker", "ps", "-aq"], STEP_SEC)
    if code != 0:
        return "red", f"docker ps вернул {code}"
    ids = out.split()
    rows = []
    if ids:
        code, out = run(["docker", "inspect", "--format",
                         "{{.Name}} {{.HostConfig.RestartPolicy.Name}} {{.State.Running}}", *ids], STEP_SEC)
        if code != 0:
            return "red", f"docker inspect вернул {code}"
        rows = [line.split() for line in out.splitlines() if len(line.split()) == 3]
    watched = [(name.lstrip("/"), running) for name, policy, running in rows if policy in POLICIES_UP]
    down = [name for name, running in watched if running != "true"]
    up = len(watched) - len(down)
    if down:
        return "red", f"стоят: {', '.join(down)}"
    if up < int(entry.get("не_меньше") or 0):
        return "red", f"бежит {up}, нужно не меньше {entry['не_меньше']}"
    return "ok", f"бежит {up}"


def check_http(entry, mark):
    address = entry.get("адрес") or (setting(entry["ключ"]) if entry.get("ключ") else "") or entry.get("умолчание")
    if not address:
        return "skip", f"адрес не задан ({entry.get('ключ', 'адрес')} пуст)"
    allowed = entry.get("коды")
    try:
        with urllib.request.urlopen(address, timeout=STEP_SEC) as answer:
            status = answer.status
    except urllib.error.HTTPError as error:
        status = error.code
    except (urllib.error.URLError, OSError) as error:
        return "red", f"{address}: {type(error).__name__}"
    good = status in allowed if allowed else 200 <= status < 400
    return ("ok" if good else "red"), f"{address} → {status}"


def check_command(entry, mark):
    code, out = run([str(a) for a in entry["argv"]], int(entry.get("предел_сек") or COMMAND_LIMIT_SEC),
                    cwd=setting("PROJECT_DIR") or None)
    last = out.splitlines()[-1] if out else f"код {code}"
    return ("ok" if code == 0 else "red"), last


def check_kernel(entry, mark):
    if not mark or not mark.get("ядро_ожидаемое"):
        return "skip", "метки нет, ядру не с чем сравниться"
    code, out = run(["uname", "-r"], STEP_SEC)
    if out.startswith(mark["ядро_ожидаемое"]):
        return "ok", out
    return "red", f"ядро {out}, ждали {mark['ядро_ожидаемое']}"


def load_stall_in_log():
    """Правило «ответил ли» — прибора stall-in-log, второго здесь не заводим."""
    spec = importlib.util.spec_from_file_location("stall_in_log", HERE / "stall-in-log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def first_user_after(stall, path, moment: float):
    stamps = [stall._epoch(r.get("timestamp")) for r in stall._lines(path, ('"user"',))
              if r.get("type") == "user"]
    later = [s for s in stamps if s is not None and s > moment]
    return min(later) if later else None


def check_answer(entry, mark):
    stall = load_stall_in_log()
    folder = Path(os.environ["HARNESS_BOOT_CHECK_TRANSCRIPTS"]) if os.environ.get("HARNESS_BOOT_CHECK_TRANSCRIPTS") \
        else stall.project_transcripts()
    deadline = time.time() + int(entry.get("ждать_сек") or ANSWER_WAIT_DEFAULT_SEC)
    boot = boot_time()
    detail = "транскрипта смены нет"
    while True:
        path = stall.shift_transcript(folder) if folder is not None and folder.is_dir() else None
        if path is None:
            detail = "транскрипта смены нет"
        else:
            kick = first_user_after(stall, path, boot)
            if kick is None:
                detail = "после загрузки смене не писали"
            elif stall.answered_after(path, kick, time.time()):
                return "ok", "смена ответила на пинок"
            else:
                detail = "пинок был, ответа смены нет"
        if time.time() >= deadline:
            return "red", detail
        time.sleep(min(ANSWER_POLL_SEC, max(0.0, deadline - time.time())))


CHECKS = {"юнит": check_unit, "tmux": check_tmux, "docker": check_docker, "http": check_http,
          "команда": check_command, "ядро": check_kernel, "ответ_сессии": check_answer}


def boot_time() -> float:
    if os.environ.get("HARNESS_BOOT_CHECK_BOOT_TIME"):
        return float(os.environ["HARNESS_BOOT_CHECK_BOOT_TIME"])
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise OSError("в /proc/stat нет btime")


def run_checks(entries: list, mark) -> tuple[list, list]:
    """→ (строки печати, итоги по записям)."""
    lines, outcomes = [], []
    for entry in entries:
        state, detail = CHECKS[entry["вид"]](entry, mark)
        outcomes.append((entry["имя"], state))
        if state == "ok":
            lines.append(f"{entry['имя']}: ок — {detail}")
        elif state == "red":
            door = f"; дверь: {entry['дверь']}" if entry.get("дверь") else ""
            lines.append(f"{entry['имя']}: КРАСНЫЙ — {detail}{door}")
        else:
            lines.append(f"{entry['имя']}: не проверено — {detail}")
    return lines, outcomes


def summary(outcomes: list) -> tuple[str, bool]:
    names = lambda wanted: [n for n, s in outcomes if s == wanted]
    red, skipped = names("red"), names("skip")
    if red:
        return f"НЕ ПОДНЯЛСЯ: красных {len(red)} из {len(outcomes)}: {', '.join(red)}", False
    text = f"поднялся: ок {len(names('ok'))} из {len(outcomes)}"
    if skipped:
        text += f"; не проверено {len(skipped)}: {', '.join(skipped)}"
    return text, True


# ── занято ──

def expand(text: str) -> str:
    def value(found):
        result = setting(found.group(1))
        if not result:
            raise BrokenList(f"перечень сломан: ключ {found.group(1)} пуст в «{text}»")
        return result
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value, text)


def busy_lock(entry) -> bool:
    path = Path(expand(str(entry["файл"])))
    if not path.exists():
        return False
    with open(path, "rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def pgrep(flag: str, pattern: str) -> bool:
    code, out = run(["pgrep", flag, pattern], STEP_SEC)
    if code not in (0, 1):
        raise BrokenList(f"pgrep {pattern} вернул {code}")
    ours = {str(os.getpid()), str(os.getppid())}
    return code == 0 and bool(set(out.split()) - ours)


def busy_process(entry) -> bool:
    return any(pgrep("-x", str(name)) for name in entry["имена"])


def busy_by_line(entry) -> bool:
    return pgrep("-f", str(entry["образец"]))


BUSY = {"замок": busy_lock, "процесс": busy_process, "процесс_по_строке": busy_by_line}


def command_busy(path: Path) -> int:
    try:
        entries = entries_of(load_list(path), "занято", BUSY_KINDS)
        busy = [e["имя"] for e in entries if BUSY[e["вид"]](e)]
    except (BrokenList, OSError) as error:
        print(f"неизвестно: {error}")
        return 2
    print(f"занята: {', '.join(busy)}" if busy else "свободна")
    return 1 if busy else 0


# ── метка и итог ──

def write_json(path: Path, data: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def finish(mark_path: Path, mark: dict, result: str, kernel_changed) -> None:
    """Итог рядом с меткой; метку снимает только эта функция."""
    write_json(mark_path.with_name("reboot-last.json"),
               dict(mark, **{"проверено": int(time.time()), "итог": result, "ядро_сменилось": kernel_changed}))
    mark_path.unlink()


def kernel_changed(mark: dict) -> bool:
    _, out = run(["uname", "-r"], STEP_SEC)
    return out.startswith(mark.get("ядро_ожидаемое") or "\0")


def judge_with_mark(entries: list, mark_path: Path, mark: dict) -> int:
    lines, outcomes = run_checks(entries, mark)
    text, green = summary(outcomes)
    print("\n".join([*lines, text]))
    finish(mark_path, mark, "поднялся" if green else "красный", kernel_changed(mark))
    return 0 if green else 1


def command_mark(path: Path, mark_path: Path, late: bool) -> int:
    lock_path = mark_path.with_name(mark_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not mark_path.exists():
            return 5      # другой вызывающий уже доложил
        try:
            mark = json.loads(mark_path.read_text(encoding="utf-8"))
            started = float(mark["с"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"метка не читается: {type(error).__name__}")
            return 2
        late_sec = int(setting("REBOOT_LATE_MIN") or LATE_MIN_DEFAULT) * 60
        now, booted = time.time(), boot_time()
        if booted <= started:
            if now - started < late_sec:
                print("рано: загрузки после метки ещё нет")
                return 5
            reason = f" (команда вернула {mark['команда_код']})" if mark.get("команда_код") else ""
            print(f"перезагрузка не состоялась{reason}")
            finish(mark_path, mark, "не состоялась", None)
            return 1
        if late and now - booted < late_sec:
            print("рано: свежую загрузку ведёт --boot")
            return 5
        entries = entries_of(load_list(path), "проверки", KINDS)
        check_budget(entries)
        return judge_with_mark(entries, mark_path, mark)


def command_manual(path: Path) -> int:
    entries = entries_of(load_list(path), "проверки", KINDS)
    check_budget(entries)
    lines, outcomes = run_checks(entries, None)
    text, green = summary(outcomes)
    print("\n".join([*lines, text]))
    return 0 if green else 1


def main(argv: list) -> int:
    args = list(argv)
    flags = {"--занято": False, "--поздно": False}
    values = {"--метка": None, "--перечень": None}
    while args:
        word = args.pop(0)
        if word in flags:
            flags[word] = True
        elif word in values and args:
            values[word] = args.pop(0)
        else:
            print(f"незнакомый довод: {word}", file=sys.stderr)
            return 2
    path = Path(values["--перечень"]) if values["--перечень"] else default_list()
    if flags["--занято"]:
        return command_busy(path)
    try:
        if values["--метка"]:
            return command_mark(path, Path(values["--метка"]), flags["--поздно"])
        return command_manual(path)
    except BrokenList as error:
        print(error)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
