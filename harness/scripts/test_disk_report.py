#!/usr/bin/env python3
"""Пробы тревоги «диск занят» и описи диска (Ф1 задачи pb.raise-disk-autocleanup-efficiency).

Что судится (спека §0, §3, §4.1 сост. 4-7, §5, §7.7; контракт pretask.yaml, Р-1, Р-4, Р-8, Р-9):
одна весть на пересечение порога; повтор — через DISK_ALERT_REPEAT_H или при росте на 3 п.п.;
спад ниже порога снимает метку (перевзвод) — на почасовом прогоне тоже, ДО раннего выхода;
в тексте вести — опись «где лежит место», простыми словами и под потолок канала, без вывода
`docker system df` и без имён записей /tmp; недоставка и лимит откладывают весть, а не
пропускают и не удваивают; Ф1 ничего не удаляет.

Двери, которыми проба зовёт механизм, — те же, что у боевого хода:
  * `scripts/disk-report.py` РЯДОМ с пробой (мутации подставляют копию дерева, «из-копии»):
    CLI `--тревога --занято N --освобождено МБ` → первая строка `весть: …` (код 0, ниже —
    текст вести) | `молчу: тревога уже отправлена в ЧЧ:ММ при N %` | `перевзвод: метка снята` |
    `ниже порога` (код 3); `--отправлено --занято N` пишет метку; `--опись` — строки
    `класс<TAB>МБ<TAB>чьё<TAB>решение` и итог `опись: N классов, M МБ`.
    Функции `read_mark(path)` и `decide(pct, mark, now, max_pct=, repeat_h=)` — только там,
    где нужен `now` аргументом (граница повтора). Решение судится по полю `kind` (или первому
    элементу кортежа): одно из «весть», «молчу», «перевзвод», «ниже порога».
  * `harness/demons/disk-cleanup.sh` (дверь `HARNESS_DISK_CLEANUP` — как у пробы двери канала).
Метка — `$HEARTBEAT_DIR/disk-alert`, JSON {"процент": N, "когда": "<ISO с поясом>"}.

Стенд — свой `mkdtemp(prefix="тревога-стенд-")` в /tmp на КАЖДЫЙ случай; конфиги, LOG_DIR,
HEARTBEAT_DIR, PROJECT_DIR, HOME — стенда. В PATH первым стоит каталог заглушек: `df` (печатает
процент стенда), `docker`, `npm`, `pip`, `uv`, `pgrep`, `sudo`, `apt-get`, `du`, `journalctl`, а
для disk-report.py ещё `rm`, `rmdir`, `shred`, `unlink`, `truncate` — каждая пишет вызов в
журнал стенда и ничего не делает. `du` и `sudo` механизм зовёт ПО ИМЕНИ из PATH. Перед первым
прогоном уборки проба сверяет `command -v` каждого из 10 имён → стенд; иначе ПРОВАЛ без прогона.
Шаг 10 уборки (scripts/tmp-sweep.py) зовётся дверью TMP_SWEEP_SCRIPT → заглушка стенда: настоящая
уборка /tmp ходит по константе ROOT=/tmp, и стенд её не изолирует. Страж: до каждого прогона уборки
дверь обязана вести в заглушку внутри стенда (иначе «ПЛОХО СТЕНД» и выход без прогона), после
каждого случая в calls.log только вызовы заглушки и нет журнала настоящей уборки (уборка-tmp.jsonl).
Боевой /tmp, живой LOG_DIR и /etc/harness не пишутся. Стенды убираются в конце случая и ДО
работы (только стенды умерших прогонов — по pid-файлу стенда).

Модуля нет — случаи disk-report.py красные «нет модуля»; случаи уборки (A6, A10-A13) идут на
стенде и красны поведением нынешней уборки.
Итог: «ТРЕВОГА ДИСКА: случаев N, неудач M», код 0/1.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE / "disk-report.py"
CHANNEL_GUARD = HERE / "check-channel-messages.py"
LANGUAGE_LEVEL = HERE / "language-level.py"
STAND_PREFIX = "тревога-стенд-"
STUB_NAMES = ("df", "docker", "npm", "pip", "uv", "pgrep", "sudo", "apt-get", "du", "journalctl")
DELETE_STUBS = ("rm", "rmdir", "shred", "unlink", "truncate")
MAX_PCT = 85
TRIGGER_PCT = 80
REPEAT_H = 24
SECRET_ENTRY = "ключ-XYZ7Q2"          # не-секрет: имя записи /tmp на стенде, в канал и в опись не попадает
CLI_TIMEOUT = 60
CLEANUP_TIMEOUT = 120
SWEEP_STUB_MARK = "ЗАГЛУШКА-СТЕНДА"
SWEEP_JOURNAL = "уборка-tmp.jsonl"     # след настоящей tmp-sweep.py в LOG_DIR

# Таблица `du -x -k` стенда (КБ, путь): с родителями, как печатает настоящий du.
DU_TABLE = (
    (33600000, "/"),
    (19500000, "/var"),
    (9800000, "/var/snap"),
    (9800000, "/var/snap/lxd"),
    (4300000, "/var/www"),
    (4300000, "/var/www/site-x"),
    (2900000, "/var/lib"),
    (2900000, "/var/lib/docker"),
    (1900000, "/var/log"),
    (1800000, "/var/log/journal"),
    (720000, "/var/cache"),
    (716000, "/var/cache/apt"),
    (716000, "/var/cache/apt/archives"),
    (6200000, "/tmp"),
    (5000000, f"/tmp/{SECRET_ENTRY}"),
    (5200000, "/home"),
    (5200000, "/home/user"),
    (4900000, "/home/user/android"),
)


def du_kb(path: str) -> int:
    return dict((p, kb) for kb, p in DU_TABLE)[path]


class Probe:
    def __init__(self) -> None:
        self.cases = 0
        self.checks = 0
        self.failed_cases = 0
        self._case_failed = False

    def start(self, label: str, title: str) -> None:
        self.cases += 1
        self._case_failed = False
        print(f"{label} {title}")

    def check(self, label: str, name: str, ok: bool, detail: str = "") -> bool:
        self.checks += 1
        if ok:
            print(f"  ок    {label} {name}")
        else:
            self._case_failed = True
            print(f"  ПЛОХО {label} {name}: {str(detail)[:600]}")
        return ok

    def fail(self, label: str, why: str) -> None:
        self.check(label, "случай не дошёл до суда", False, why)

    def finish(self) -> None:
        if self._case_failed:
            self.failed_cases += 1


PROBE = Probe()


# ─────────────────────────────── стенд ───────────────────────────────────────

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_dead_stands() -> int:
    """Стенды умерших прогонов (SIGKILL пропускает finally). Живой чужой прогон не трогается."""
    removed = 0
    for path in glob.glob(f"/tmp/{STAND_PREFIX}*"):
        try:
            pid = int(Path(path, "pid").read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and pid_alive(pid):
            continue
        if pid is None and time.time() - os.lstat(path).st_mtime < 3600:
            continue        # pid ещё не записан — стенд рождается прямо сейчас
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


STUB_BODY = {
    "df": r'''p=$(cat "$S/pct")
case "$*" in
  *pcent*) printf 'Use%%\n %s%%\n' "$p" ;;
  *used*)  printf '     Used\n%s\n' "$((p * 1000))" ;;
  *)       printf 'Filesystem 1K-blocks Used Available Use%% Mounted on\n/dev/stand 100000 %s 0 %s%% /\n' "$((p * 1000))" "$p" ;;
esac''',
    "du": r'''cat "$S/du-table"''',
    "sudo": r'''if [ -e "$S/sudo-refuse" ]; then echo "sudo: a password is required" >&2; exit 1; fi
while [ $# -gt 0 ]; do case "$1" in -u|-g) shift 2 ;; -*) shift ;; *) break ;; esac; done
exec "$@"''',
    "docker": r'''case "$*" in
  *"system df"*) printf 'TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE\nImages          3         1         2.9GB     1.1GB (37%%)\n' ;;
esac
exit 0''',
    "journalctl": r'''case "$*" in
  *--disk-usage*) echo "Archived and active journals take up 1.8G in the file system." ;;
esac
exit 0''',
    "pgrep": "exit 1",
}


def write_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def make_stand() -> Path:
    stand = Path(tempfile.mkdtemp(prefix=STAND_PREFIX, dir="/tmp"))
    (stand / "pid").write_text(str(os.getpid()))
    for sub in ("bin", "etc", "log", "heartbeat", "home", "project/scripts", "project/память",
                "msgs", "decoy/log"):
        (stand / sub).mkdir(parents=True)
    (stand / "calls.log").write_text("")
    (stand / "pct").write_text("50")
    (stand / "tg-mode").write_text("ok")
    (stand / "du-table").write_text("".join(f"{kb}\t{p}\n" for kb, p in DU_TABLE))
    write_exec(stand / "project/scripts/tmp-sweep.py",
               "#!/usr/bin/env python3\n"
               "import sys\n"
               f"with open({str(stand / 'calls.log')!r}, 'a', encoding='utf-8') as log:\n"
               f"    log.write('tmp-sweep\\t{SWEEP_STUB_MARK} вызван ' + ' '.join(sys.argv[1:]) + '\\n')\n")
    # Шаг 10в зовёт gates-snapshot.sh --убрать: без заглушки пошёл бы настоящий скрипт.
    write_exec(stand / "project/scripts/gates-snapshot.sh",
               "#!/bin/bash\n"
               f"printf 'gates-snapshot\\t%s\\n' \"$*\" >> '{stand}/calls.log'\n"
               "echo 'снимки ворот: убрано 2, 5 МБ; осталось 1 (самый свежий — всегда)'\n")
    for name in STUB_NAMES + DELETE_STUBS:
        body = STUB_BODY.get(name, "exit 0")
        write_exec(stand / "bin" / name,
                   "#!/bin/bash\n"
                   f"S='{stand}'\n"
                   f"printf '%s\\t%s\\n' '{name}' \"$*\" >> \"$S/calls.log\"\n"
                   f"{body}\n")
    write_exec(stand / "project/scripts/tg_send.sh", f"""#!/bin/bash
S='{stand}'
mode=$(cat "$S/tg-mode")
case "$mode" in
  refuse) echo "tg_send: ОТКАЗ транспорта" >&2; exit 1 ;;
  held)   printf '%s\\n---\\n' "$1" >> "$S/held.log"
          echo "tg_send: ПРИДЕРЖАНО — держится лимит подписки; уйдёт досылом после обновления." >&2
          exit 0 ;;
esac
n=$(ls "$S/msgs" | wc -l)
printf '%s' "$1" > "$S/msgs/$((n + 1)).txt"
""")
    (stand / "project/память/TONE.md").write_text("<!-- потолки: мягкий=700 жёсткий=1400 -->\n",
                                                  encoding="utf-8")
    (stand / "etc/install.conf").write_text(
        f'PROJECT_DIR="{stand}/project"\nLOG_DIR="{stand}/log"\n'
        f'HEARTBEAT_DIR="{stand}/heartbeat"\nTG_CHAT_ID=""\nTG_TOKEN_FILE=""\n', encoding="utf-8")
    set_conf(stand, REPEAT_H)
    # Приманка «удалять нечего»: старый журнал и кеш — Ф1 их не касается.
    old = stand / "decoy/log/old-run.log"
    old.write_text("старый прогон\n")
    os.utime(old, (time.time() - 100 * 86400,) * 2)
    (stand / "home/.npm/_cacache").mkdir(parents=True)
    (stand / "home/.npm/_cacache/index").write_text("кеш\n")
    return stand


def set_conf(stand: Path, repeat_h: float, extra: str = "") -> None:
    (stand / "etc/harness.conf").write_text(
        f"DISK_MAX_PCT={MAX_PCT}\nDISK_TRIGGER_PCT={TRIGGER_PCT}\nDISK_ALERT_REPEAT_H={repeat_h}\n{extra}",
        encoding="utf-8")


def stand_env(stand: Path) -> dict:
    py_dir = str(Path(sys.executable).parent)
    return {
        "PATH": f"{stand}/bin:{py_dir}:/usr/local/bin:/usr/bin:/bin",
        "HOME": f"{stand}/home",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HARNESS_HOME": f"{stand}/etc",
        "HARNESS_INSTALL_CONF": f"{stand}/etc/install.conf",
        "HARNESS_CONF": f"{stand}/etc/harness.conf",
        "PROJECT_DIR": f"{stand}/project",
        "LOG_DIR": f"{stand}/log",
        "HEARTBEAT_DIR": f"{stand}/heartbeat",
        "TMP_SWEEP_SCRIPT": f"{stand}/project/scripts/tmp-sweep.py",
        "GATES_SNAPSHOT_SCRIPT": f"{stand}/project/scripts/gates-snapshot.sh",
    }


def mark_path(stand: Path) -> Path:
    return stand / "heartbeat" / "disk-alert"


def write_mark(stand: Path, pct, when: float) -> None:
    iso = dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat(timespec="seconds")
    mark_path(stand).write_text(json.dumps({"процент": pct, "когда": iso}, ensure_ascii=False),
                                encoding="utf-8")


def calls(stand: Path) -> list:
    return [line.split("\t", 1) + [""] for line in
            (stand / "calls.log").read_text(encoding="utf-8").splitlines() if line]


def count_calls(stand: Path, name: str) -> int:
    return sum(1 for c in calls(stand) if c[0] == name)


def destructive_calls(stand: Path) -> list:
    """Вызовы, которыми Ф1 НЕ имеет права пользоваться: удаление, чистка кешей, вакуум."""
    bad = []
    for name, args, *_ in calls(stand):
        if (name in DELETE_STUBS
                or (name == "apt-get" and re.search(r"\b(auto)?clean\b|\bremove\b|\bpurge\b", args))
                or (name == "journalctl" and "vacuum" in args)
                or (name == "docker" and re.search(r"\bprune\b|\brmi?\b", args))
                or (name in ("npm", "pip", "uv"))):
            bad.append(f"{name} {args}")
    return bad


def tree_hash(root: Path) -> tuple:
    digest = hashlib.sha256()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath, name)
            st = os.lstat(path)
            count += 1
            digest.update(str(path.relative_to(root)).encode())
            digest.update(str(stat.S_IMODE(st.st_mode)).encode())
            if stat.S_ISREG(st.st_mode):
                digest.update(path.read_bytes())
    return digest.hexdigest(), count


# ─────────────────────────────── двери ───────────────────────────────────────

class Run:
    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.rc = proc.returncode
        self.out = proc.stdout.decode("utf-8", "replace")
        self.err = proc.stderr.decode("utf-8", "replace")
        lines = self.out.splitlines()
        self.first = lines[0] if lines else ""
        self.rest = "\n".join(lines[1:]).strip()

    def __repr__(self) -> str:
        return f"rc={self.rc} out={self.out[:300]!r} err={self.err[-300:]!r}"


def report(stand: Path, *args: str) -> Run:
    return Run(subprocess.run([sys.executable, "-B", str(REPORT), *args], cwd=stand,
                              env=stand_env(stand), capture_output=True, timeout=CLI_TIMEOUT))


def alarm(stand: Path, pct: int, freed_mb: int = 0) -> Run:
    return report(stand, "--тревога", "--занято", str(pct), "--освобождено", str(freed_mb))


def sent(stand: Path, pct: int) -> Run:
    return report(stand, "--отправлено", "--занято", str(pct))


def verdict(run: Run) -> str:
    match = re.match(r"(весть|молчу|перевзвод|ниже порога)", run.first)
    return match.group(1) if match else f"?{run.first!r}"


def cleanup_script() -> Path:
    door = os.environ.get("HARNESS_DISK_CLEANUP")
    if door:
        return Path(door).resolve()
    for candidate in (HERE.parent / "harness/demons/disk-cleanup.sh",
                      HERE.parent / "demons/disk-cleanup.sh"):
        if candidate.is_file():
            return candidate.resolve()
    raise AssertionError("не найден disk-cleanup.sh рядом с пробой и нет HARNESS_DISK_CLEANUP")


def stubs_in_front(stand: Path) -> str:
    """Пусто, если все 10 имён ведут в заглушки стенда; иначе — что ушло бы в систему."""
    script = 'for n in "$@"; do printf "%s\\t%s\\n" "$n" "$(command -v "$n")"; done'
    out = subprocess.run(["bash", "-c", script, "_", *STUB_NAMES], env=stand_env(stand),
                         capture_output=True, text=True, timeout=CLI_TIMEOUT).stdout
    bad = []
    for line in out.splitlines():
        name, _, where = line.partition("\t")
        if where != f"{stand}/bin/{name}":
            bad.append(f"{name} → {where or 'нет'}")
    if len(out.splitlines()) != len(STUB_NAMES):
        bad.append(f"ответов {len(out.splitlines())} из {len(STUB_NAMES)}")
    return "; ".join(bad)


def sweep_door_off_stand(stand: Path, script: Path) -> str:
    """Пусто, если шаг 10 уборки уйдёт в заглушку стенда; иначе — куда ушёл бы."""
    door = stand_env(stand).get("TMP_SWEEP_SCRIPT", "")
    target = Path(door).resolve() if door else None
    if target is None or not target.is_relative_to(stand.resolve()):
        return f"TMP_SWEEP_SCRIPT={door or 'нет'} вне стенда {stand}"
    if not target.is_file() or SWEEP_STUB_MARK not in target.read_text(encoding="utf-8"):
        return f"{target} — не заглушка стенда"
    if "TMP_SWEEP_SCRIPT" not in script.read_text(encoding="utf-8"):
        return f"{script} не читает дверь TMP_SWEEP_SCRIPT — пошёл бы настоящий tmp-sweep.py"
    return ""


def sweep_guard_after(stand: Path) -> str:
    """Пусто, если за случай настоящая уборка /tmp не ходила."""
    bad = [f"{n} {a}" for n, a, *_ in calls(stand) if n == "tmp-sweep" and not a.startswith(SWEEP_STUB_MARK)]
    if (stand / "log" / SWEEP_JOURNAL).exists():
        bad.append(f"в LOG_DIR стенда журнал настоящей уборки {SWEEP_JOURNAL}")
    return "; ".join(bad)


def cleanup(stand: Path, *args: str, stdout=None) -> Run:
    script = cleanup_script()
    off = sweep_door_off_stand(stand, script)
    if off:
        print(f"ПЛОХО СТЕНД: {off} — disk-cleanup.sh не запущен, проба остановлена")
        raise SystemExit(2)
    proc = subprocess.run(["bash", str(script), *args], cwd=stand, env=stand_env(stand),
                          stdout=stdout if stdout is not None else subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=CLEANUP_TIMEOUT)
    if proc.stdout is None:
        proc.stdout = b""
    return Run(proc)


def messages(stand: Path) -> list:
    return [p.read_text(encoding="utf-8") for p in
            sorted((stand / "msgs").iterdir(), key=lambda p: int(p.stem))]


def need_module(label: str) -> bool:
    if REPORT.is_file():
        return True
    PROBE.check(label, "есть модуль", False, f"нет модуля {REPORT}")
    return False


def load_report():
    spec = importlib.util.spec_from_file_location("disk_report", REPORT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def kind_of(decision) -> str:
    raw = getattr(decision, "kind", None)
    if raw is None and isinstance(decision, (tuple, list)) and decision:
        raw = decision[0]
    match = re.match(r"(весть|молчу|перевзвод|ниже порога)", str(raw))
    return match.group(1) if match else f"?{decision!r}"


def gb_spellings(kb: int) -> set:
    """Как человек прочтёт размер в ГБ: двоичные и десятичные, округление и отсечение."""
    out = set()
    for value in (kb / 1048576, kb * 1024 / 1e9):
        for rounded in (round(value, 1), int(value * 10) / 10):
            out.add(f"{rounded:.1f}".replace(".", ","))
    return out


# ─────────────────────────────── случаи ──────────────────────────────────────

def case_a1(stand: Path) -> None:
    """§7.7: 86 % трижды → 1 весть; 89 → вторая; метка 25 ч → третья; 80 → перевзвод; 86 → четвёртая."""
    if not need_module("A1"):
        return
    seq = []

    def step(pct: int, deliver: bool = True) -> Run:
        run = alarm(stand, pct)
        seq.append((pct, verdict(run), run.rc))
        if verdict(run) == "весть" and deliver:
            sent(stand, pct)
        return run

    step(86)
    m = mark_path(stand)
    try:
        mark = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        mark = {"ошибка": str(error)}
    when = mark.get("когда", "")
    try:
        age = time.time() - dt.datetime.fromisoformat(when).timestamp()
        has_tz = dt.datetime.fromisoformat(when).tzinfo is not None
    except (TypeError, ValueError):
        age, has_tz = None, False
    PROBE.check("A1", "после --отправлено метка {процент: 86, когда: ISO с поясом, сейчас}",
                mark.get("процент") == 86 and has_tz and age is not None and -5 <= age <= 60, mark)
    second = step(86)
    hhmm = dt.datetime.fromisoformat(when).astimezone(dt.timezone.utc).strftime("%H:%M") if has_tz else "??"
    PROBE.check("A1", f"повтор при свежей метке — «молчу: тревога уже отправлена в {hhmm} при 86 %», код 3",
                second.first == f"молчу: тревога уже отправлена в {hhmm} при 86 %" and second.rc == 3,
                second)
    step(86)
    step(89)
    write_mark(stand, 89, time.time() - 25 * 3600)
    step(89)
    rearm = step(80)
    PROBE.check("A1", "80 % при метке — «перевзвод: метка снята», код 3, файла метки нет",
                rearm.first.startswith("перевзвод: метка снята") and rearm.rc == 3
                and not mark_path(stand).exists(), (rearm, mark_path(stand).exists()))
    step(80)
    step(86)
    kinds = [k for _, k, _ in seq]
    want = ["весть", "молчу", "молчу", "весть", "весть", "перевзвод", "ниже порога", "весть"]
    PROBE.check("A1", "таблица §7.7: весть, молчу, молчу, весть(+3), весть(25 ч), перевзвод, "
                "ниже порога, весть — итого 4 вести", kinds == want, seq)
    codes_ok = all((rc == 0) == (k == "весть") and rc in (0, 3) for _, k, rc in seq)
    PROBE.check("A1", "код 0 только у «весть», иначе 3", codes_ok, seq)


def case_a2(stand: Path) -> None:
    """Граница роста: метка 85; 87 → молчу (+2), 88 → весть (ровно +3)."""
    if not need_module("A2"):
        return
    write_mark(stand, 85, time.time() - 60)
    r87 = alarm(stand, 87)
    PROBE.check("A2", "метка 85, сейчас 87 (+2 п.п.) → «молчу»", verdict(r87) == "молчу", r87)
    r88 = alarm(stand, 88)
    PROBE.check("A2", "метка 85, сейчас 88 (+3 п.п.) → «весть»", verdict(r88) == "весть", r88)


def case_a3(stand: Path) -> None:
    """Граница повтора: ровно repeat_h → весть (≥), на 1 с раньше → молчу; порог — аргумент и ключ."""
    if not need_module("A3"):
        return
    module = load_report()
    base = float(int(time.time()) - 1000 * 3600)
    write_mark(stand, 86, base)
    mark = module.read_mark(str(mark_path(stand)))
    for repeat_h in (24, 2):
        edge = base + repeat_h * 3600
        at = kind_of(module.decide(86, mark, edge, max_pct=MAX_PCT, repeat_h=repeat_h))
        before = kind_of(module.decide(86, mark, edge - 1, max_pct=MAX_PCT, repeat_h=repeat_h))
        PROBE.check("A3", f"repeat_h={repeat_h}: метка ровно {repeat_h} ч назад → весть", at == "весть", at)
        PROBE.check("A3", f"repeat_h={repeat_h}: на 1 с моложе → молчу", before == "молчу", before)
    # Ключ конфига доходит до CLI: метка 3 ч назад — при 24 ч молчу, при 2 ч весть.
    write_mark(stand, 86, time.time() - 3 * 3600)
    r24 = alarm(stand, 86)
    set_conf(stand, 2)
    r2 = alarm(stand, 86)
    set_conf(stand, REPEAT_H)
    PROBE.check("A3", "DISK_ALERT_REPEAT_H из конфига: 24 → молчу, 2 → весть (метка 3 ч)",
                verdict(r24) == "молчу" and verdict(r2) == "весть", (r24, r2))


def case_a4(stand: Path) -> None:
    """Нечитаемая метка (не JSON, пустая, процент строкой) = метки нет: весть и «метка нечитаема»."""
    if not need_module("A4"):
        return
    iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for name, body in (("не JSON", "{процент: 86"), ("пустой файл", ""),
                       ("процент строкой", json.dumps({"процент": "86", "когда": iso}, ensure_ascii=False))):
        mark_path(stand).write_text(body, encoding="utf-8")
        run = alarm(stand, 86)
        PROBE.check("A4", f"{name}: «весть», код 0, строка «метка нечитаема», без трассы",
                    verdict(run) == "весть" and run.rc == 0 and "метка нечитаема" in run.out
                    and "Traceback" not in run.out + run.err, run)


def case_a5(stand: Path) -> None:
    """Весть не ушла (--отправлено не звали) → метки нет, следующий вызов снова «весть»."""
    if not need_module("A5"):
        return
    first = alarm(stand, 86)
    PROBE.check("A5", "--тревога сам метку не пишет", verdict(first) == "весть"
                and not mark_path(stand).exists(), (first, mark_path(stand).exists()))
    again = alarm(stand, 86)
    PROBE.check("A5", "недоставленная весть повторяется", verdict(again) == "весть", again)


def case_a6(stand: Path) -> None:
    """Р-4: почасовой прогон ниже DISK_TRIGGER_PCT снимает метку ДО раннего выхода."""
    if not REPORT.is_file():
        print(f"  (нет модуля {REPORT} — уборка судится как есть)")
    bad = stubs_in_front(stand)
    if not PROBE.check("A6", "10 имён ведут в заглушки стенда", not bad, bad):
        return
    write_mark(stand, 86, time.time() - 600)
    (stand / "pct").write_text("75")
    run = cleanup(stand, "--по-давлению")
    PROBE.check("A6", "код 0, ранний выход «убирать нечего»", run.rc == 0 and "убирать нечего" in run.out, run)
    PROBE.check("A6", "метка снята, строка «диск ниже порога, тревога перевзведена»",
                not mark_path(stand).exists() and "диск ниже порога, тревога перевзведена" in run.out,
                (mark_path(stand).exists(), run))
    PROBE.check("A6", "вестей 0", messages(stand) == [], messages(stand))


def case_a7(stand: Path) -> None:
    """Текст вести: опись вместо docker system df, простые слова, потолок канала, без имён /tmp."""
    if not need_module("A7"):
        return
    run = alarm(stand, 86, freed_mb=300)
    text = run.rest
    if not PROBE.check("A7", "«весть», код 0, текст есть", verdict(run) == "весть" and run.rc == 0
                       and len(text) > 20, run):
        return
    PROBE.check("A7", "нет вывода docker system df", "docker system df" not in text
                and "RECLAIMABLE" not in text and "TYPE" not in text, text)
    PROBE.check("A7", f"нет имени записи /tmp «{SECRET_ENTRY}»", SECRET_ENTRY not in run.out + run.err, text)
    PROBE.check("A7", "процент и убранное: «86 %», «0,3 ГБ»",
                re.search(r"86\s?%", text) is not None and "0,3 ГБ" in text, text)
    lxd = gb_spellings(du_kb("/var/snap/lxd"))
    PROBE.check("A7", f"опись: /var/snap/lxd с размером в ГБ ({' | '.join(sorted(lxd))})",
                "/var/snap/lxd" in text and any(s in text for s in lxd) and "ГБ" in text, text)
    PROBE.check("A7", "«ждёт/ждут» — что на слово владельца", re.search(r"жд[её]т|ждут", text, re.I) is not None, text)
    PROBE.check("A7", f"обещание повтора «{REPEAT_H} ч»", f"{REPEAT_H} ч" in text, text)
    guard = subprocess.run([sys.executable, "-B", str(CHANNEL_GUARD), "--текст", text],
                           env=stand_env(stand), capture_output=True, text=True, timeout=CLI_TIMEOUT)
    PROBE.check("A7", "сторож канала (потолки 700/1400 стенда): код 0, замечаний нет",
                guard.returncode == 0 and "[переписка]" not in guard.stdout and len(text) <= 700,
                (guard.returncode, guard.stdout, len(text)))
    spec = importlib.util.spec_from_file_location("language_level", LANGUAGE_LEVEL)
    level = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(level)
    terms = sorted({t.lower() for t in level.ГЛУБОКИЕ.findall(text)})
    PROBE.check("A7", "простой язык: терминов из словаря language-level нет", not terms, terms)
    PROBE.check("A7", "ничего не удалено и не чищено", not destructive_calls(stand), destructive_calls(stand))


def case_a8(stand: Path) -> None:
    """Опись: строки классов с решением; без sudo — «под правом агента: чужое не видно», код 0."""
    if not need_module("A8"):
        return
    before = tree_hash(stand / "home")
    run = report(stand, "--опись")
    rows = [line.split("\t") for line in run.out.splitlines() if line.count("\t") == 3]
    PROBE.check("A8", "код 0, строки «класс\\tМБ\\tчьё\\tрешение»", run.rc == 0 and len(rows) >= 4, run)
    ok_rows = all(r[1].isdigit() and re.fullmatch(r"убираю|хозяин \S.*|ждёт слова", r[3]) for r in rows)
    PROBE.check("A8", "МБ — целое, решение ∈ {убираю, хозяин <механизм>, ждёт слова}", ok_rows, rows)
    want_mb = du_kb("/var/snap/lxd") // 1024
    lxd = [r for r in rows if "/var/snap/lxd" in r[0]]
    PROBE.check("A8", f"чужое место названо путём: /var/snap/lxd ≈ {want_mb} МБ, «ждёт слова»",
                len(lxd) == 1 and abs(int(lxd[0][1]) - want_mb) <= 1 and lxd[0][3] == "ждёт слова", lxd)
    total = re.search(r"^опись: (\d+) классов, (\d+) МБ$", run.out, re.M)
    PROBE.check("A8", "итог «опись: N классов, M МБ» сходится со строками",
                total is not None and int(total.group(1)) == len(rows)
                and abs(int(total.group(2)) - sum(int(r[1]) for r in rows if r[1].isdigit())) <= len(rows),
                (total and total.group(0), len(rows)))
    PROBE.check("A8", f"имени записи /tmp «{SECRET_ENTRY}» в описи нет", SECRET_ENTRY not in run.out, run)
    order = [c[0] for c in calls(stand) if c[0] in ("sudo", "du")]
    PROBE.check("A8", "du зван через sudo -n", order[:2] == ["sudo", "du"]
                and any(c[0] == "sudo" and c[1].startswith("-n") for c in calls(stand)), calls(stand))
    (stand / "sudo-refuse").write_text("")
    agent = report(stand, "--опись")
    agent_rows = [line for line in agent.out.splitlines() if line.count("\t") == 3]
    PROBE.check("A8", "sudo отказал: код 0, «под правом агента: чужое не видно», строки есть",
                agent.rc == 0 and "под правом агента: чужое не видно" in agent.out and agent_rows, agent)
    PROBE.check("A8", "ничего не удалено и не чищено", not destructive_calls(stand)
                and tree_hash(stand / "home") == before, destructive_calls(stand))


def case_a9(stand: Path) -> None:
    """Р-9: опись (du) не считается, когда весть не уходит."""
    if not need_module("A9"):
        return
    write_mark(stand, 86, time.time() - 60)
    quiet = alarm(stand, 86)
    du_quiet = count_calls(stand, "du")
    rearm = alarm(stand, 50)
    below = alarm(stand, 50)
    du_after = count_calls(stand, "du")
    PROBE.check("A9", "«молчу», «перевзвод», «ниже порога» — du не звали ни разу",
                [verdict(quiet), verdict(rearm), verdict(below)] == ["молчу", "перевзвод", "ниже порога"]
                and du_quiet == 0 and du_after == 0, (quiet, rearm, below, du_after))
    loud = alarm(stand, 86)
    PROBE.check("A9", "при «весть» du зван (заглушка — носитель описи, иначе случай слеп)",
                verdict(loud) == "весть" and count_calls(stand, "du") >= 1, (loud, count_calls(stand, "du")))


def run_twice_at(stand: Path, pct: int) -> tuple:
    (stand / "pct").write_text(str(pct))
    return cleanup(stand), cleanup(stand)


def case_a10(stand: Path) -> None:
    """Уборка на стенде: 86 % два прогона → одна весть, во втором «тревога уже отправлена»."""
    if not REPORT.is_file():
        print(f"  (нет модуля {REPORT} — уборка судится как есть)")
    bad = stubs_in_front(stand)
    if not PROBE.check("A10", "10 имён ведут в заглушки стенда", not bad, bad):
        return
    decoy = (tree_hash(stand / "decoy"), tree_hash(stand / "home"))
    first, second = run_twice_at(stand, 86)
    PROBE.check("A10", "оба прогона код 0", first.rc == 0 and second.rc == 0, (first, second))
    msgs = messages(stand)
    PROBE.check("A10", "вестей ровно 1", len(msgs) == 1, msgs)
    PROBE.check("A10", "второй прогон: «тревога уже отправлена»", "тревога уже отправлена" in second.out, second)
    PROBE.check("A10", "в вести нет docker system df", all("RECLAIMABLE" not in m and "docker system df" not in m
                                                         for m in msgs), msgs)
    try:
        mark = json.loads(mark_path(stand).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        mark = {"ошибка": str(error)}
    PROBE.check("A10", "метка disk-alert: процент 86", mark.get("процент") == 86, mark)
    PROBE.check("A10", "заглушки — носитель (df и npm званы на стенде)",
                count_calls(stand, "df") > 0 and count_calls(stand, "npm") > 0, calls(stand)[:5])
    PROBE.check("A10", "приманки стенда целы (Ф1 ничего не удаляет)",
                (tree_hash(stand / "decoy"), tree_hash(stand / "home")) == decoy)


def case_a11(stand: Path) -> None:
    """ENOSPC: stdout уборки в /dev/full — код 0, метка свежести disk-cleanup поставлена."""
    if not REPORT.is_file():
        print(f"  (нет модуля {REPORT} — уборка судится как есть)")
    bad = stubs_in_front(stand)
    if not PROBE.check("A11", "10 имён ведут в заглушки стенда", not bad, bad):
        return
    (stand / "pct").write_text("50")
    started = time.time() - 2
    with open("/dev/full", "wb") as full:
        run = cleanup(stand, stdout=full)
    beat = stand / "heartbeat" / "disk-cleanup"
    PROBE.check("A11", ">/dev/full: код 0 и метка свежести disk-cleanup свежая",
                run.rc == 0 and beat.exists() and beat.stat().st_mtime >= started,
                (run.rc, beat.exists(), run.err[-300:]))


def case_a12(stand: Path) -> None:
    """Недоставка откладывает весть: метки нет, строка «отложено», следующий прогон шлёт."""
    if not REPORT.is_file():
        print(f"  (нет модуля {REPORT} — уборка судится как есть)")
    bad = stubs_in_front(stand)
    if not PROBE.check("A12", "10 имён ведут в заглушки стенда", not bad, bad):
        return
    (stand / "pct").write_text("86")
    (stand / "tg-mode").write_text("refuse")
    refused = cleanup(stand)
    PROBE.check("A12", "канал отказал: метки нет, строка «отложен…»",
                not mark_path(stand).exists() and re.search(r"отложен", refused.out) is not None,
                (mark_path(stand).exists(), refused))
    (stand / "tg-mode").write_text("ok")
    retry = cleanup(stand)
    PROBE.check("A12", "следующий прогон: весть ушла (1), метка стоит",
                retry.rc == 0 and len(messages(stand)) == 1 and mark_path(stand).exists(),
                (retry, messages(stand)))


def case_a13(stand: Path) -> None:
    """Лимит: канал придержал весть до обновления (код 0) — это доставка, второй вести нет."""
    if not REPORT.is_file():
        print(f"  (нет модуля {REPORT} — уборка судится как есть)")
    bad = stubs_in_front(stand)
    if not PROBE.check("A13", "10 имён ведут в заглушки стенда", not bad, bad):
        return
    (stand / "tg-mode").write_text("held")
    first, second = run_twice_at(stand, 86)
    held = (stand / "held.log").read_text(encoding="utf-8").count("\n---\n") \
        if (stand / "held.log").exists() else 0
    PROBE.check("A13", "придержано 1 раз, второй прогон «тревога уже отправлена», метка стоит",
                held == 1 and "тревога уже отправлена" in second.out and mark_path(stand).exists(),
                (held, second))


def case_a14(stand: Path) -> None:
    """Шаг 10 уборки: TMP_SWEEP_ENABLED=1 — заглушка tmp-sweep звана с --ночь; 0 — не звана."""
    bad = stubs_in_front(stand)
    if not PROBE.check("A14", "10 имён ведут в заглушки стенда", not bad, bad):
        return
    (stand / "pct").write_text("50")
    seen = {}
    for enabled in ("1", "0"):
        set_conf(stand, REPEAT_H, f"TMP_SWEEP_ENABLED={enabled}\n")
        before = count_calls(stand, "tmp-sweep")
        run = cleanup(stand)
        seen[enabled] = (run.rc, count_calls(stand, "tmp-sweep") - before)
    sweeps = [a for n, a, *_ in calls(stand) if n == "tmp-sweep"]
    PROBE.check("A14", "TMP_SWEEP_ENABLED=1: код 0, заглушка звана 1 раз с «--ночь»",
                seen["1"] == (0, 1) and len(sweeps) >= 1 and "--ночь" in sweeps[0].split(), (seen, sweeps))
    PROBE.check("A14", "TMP_SWEEP_ENABLED=0: код 0, заглушка не звана", seen["0"] == (0, 0), (seen, sweeps))


def case_a15(stand: Path) -> None:
    """Шаг 10в: снимки ворот убираются тем же выключателем, что /tmp, и не в сухом ходе."""
    (stand / "pct").write_text("50")
    seen = {}
    for enabled, args in (("1", ()), ("1", ("--dry-run",)), ("0", ())):
        set_conf(stand, REPEAT_H, f"TMP_SWEEP_ENABLED={enabled}\n")
        before = count_calls(stand, "gates-snapshot")
        run = cleanup(stand, *args)
        seen[(enabled, args)] = (run.rc, count_calls(stand, "gates-snapshot") - before,
                                 "10в. снимки ворот: убрано 2, 5 МБ" in run.out)
    snaps = [a for n, a, *_ in calls(stand) if n == "gates-snapshot"]
    PROBE.check("A15", "TMP_SWEEP_ENABLED=1: заглушка снимков звана 1 раз с «--убрать», итог в выводе",
                seen[("1", ())] == (0, 1, True) and snaps[:1] == ["--убрать"], (seen, snaps))
    PROBE.check("A15", "сухой ход: снимки ворот не убираются", seen[("1", ("--dry-run",))][:2] == (0, 0),
                seen)
    PROBE.check("A15", "TMP_SWEEP_ENABLED=0: снимки ворот не убираются", seen[("0", ())][:2] == (0, 0),
                seen)


CASES = (
    ("A1", "таблица §7.7 — одна весть на пересечение", case_a1),
    ("A2", "граница роста +3 п.п.", case_a2),
    ("A3", "граница повтора DISK_ALERT_REPEAT_H", case_a3),
    ("A4", "нечитаемая метка", case_a4),
    ("A5", "метка только после доставки", case_a5),
    ("A6", "перевзвод на почасовом прогоне до раннего выхода", case_a6),
    ("A7", "текст вести", case_a7),
    ("A8", "опись", case_a8),
    ("A9", "опись не считается без вести", case_a9),
    ("A10", "уборка: одна весть за два прогона", case_a10),
    ("A11", "вывод в /dev/full", case_a11),
    ("A12", "отказ канала откладывает весть", case_a12),
    ("A13", "лимит придержал весть — не повтор", case_a13),
    ("A14", "шаг 10 уборки /tmp по выключателю", case_a14),
    ("A15", "шаг 10в снимков ворот по выключателю", case_a15),
)


def main() -> int:
    removed = sweep_dead_stands()
    print(f"модуль: {REPORT}" + ("" if REPORT.is_file() else " — НЕТ"))
    print(f"стендов умерших прогонов убрано: {removed}")
    for label, title, case in CASES:
        PROBE.start(label, title)
        stand = None
        try:
            stand = make_stand()
            case(stand)
        except Exception as error:      # поломка пробы или механизма — красный случай, не трасса
            PROBE.fail(label, f"{type(error).__name__}: {error}")
        finally:
            if stand is not None:
                leak = sweep_guard_after(stand)
                PROBE.check(label, "страж: настоящая уборка /tmp не ходила", not leak, leak)
                shutil.rmtree(stand, ignore_errors=True)
            PROBE.finish()
    print(f"проверок {PROBE.checks}")
    print(f"ТРЕВОГА ДИСКА: случаев {PROBE.cases}, неудач {PROBE.failed_cases}")
    return 0 if PROBE.failed_cases == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
