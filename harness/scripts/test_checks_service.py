#!/usr/bin/env python3
"""Проба службы потока проверок (hr.process-dispatcher, шаг Г, спека §Г.1).

Поток проверок уходит из крона в systemd: срез harness-checks.slice с общим
потолком ядер и памяти и по паре harness-<id>.service + .timer на каждую запись
библиотеки с признаком `запуск: "служба-проверок"`. Расписание остаётся в
process-library.yaml, таймер ПОРОЖДАЕТСЯ из него: таймер, написанный руками,
разойдётся с библиотекой молча. Выключатель BACKGROUND_PROCESSES службу не
гасит, но события process.start/end обёртка пишет — иначе диспетчер (В2) слеп.

Список id судится РАЗБОРОМ библиотеки по признаку, а не вшитым перечнем;
пустой разбор — красный. Второй проход стенда меняет расписания и ставит
признак записи, которой в живой библиотеке его нет, и даёт другие потолки:
таймер или потолок, зашитые в код, а не взятые из данных, на нём краснеют.

Случаи:
  0 разбор библиотеки: id с признаком есть, у каждого — расписание в YAML;
  1 у каждого id есть service+timer, Slice=harness-checks.slice, OnCalendar
    равен расписанию библиотеки (по ближайшим срабатываниям: systemd-analyze
    calendar против разбора крон-строки), Persistent=false;
  2 в срезе CPUQuota/MemoryHigh/MemoryMax — числа из конфига; systemd-analyze
    verify без замечаний на наших файлах;
  3 cron_lines (функция и CLI установщика) не выдаёт ни одного id с признаком,
    а прочие задания выдаёт;
  4 run-background.sh при off: запись с признаком идёт (след-файл) и пишет
    process.start/end; без признака — process.skip, следа нет;
  5 USTANOVIT.sh --dry-run пишет срез, юниты, узкую строку sudoers и
    daemon-reload; строка службы в 50-agent — ровно daemon-reload и шесть форм
    на таймер (с enable --now / disable --now), без `*`; установщик сам
    включает все таймеры (включить_таймеры) и проверяет «все включены», в
    «ОСТАЛОСЬ РУКАМИ» таймеров нет;
  6 перевод календаря: формы крона вне живой библиотеки (*/n в дне и месяце,
    день недели от 0, список с 7, шаг в минутах) systemd принимает, и два
    ближайших срабатывания совпадают с кроном; непереводимые — ValueError;
  7 синтаксис под Python 3.10 (пакет обещает Ubuntu 22.04): в f-строке нет
    той же кавычки внутри {…} (PEP 701 — только с 3.12).

Стенд — свой временный каталог: копии библиотеки через
HARNESS_PROCESS_LIBRARY / HARNESS_PROCESS_LIBRARY_JSON, свой HARNESS_CONF,
HARNESS_HOME, HARNESS_INSTALL_CONF и LOG_DIR (дверь каталога журналов обёртки:
konf_zagruzit не перекрывает значение окружения). Боевые журнал, crontab,
юниты и конфиг не трогаются.

Механизмы проба находит РЯДОМ С СОБОЙ (подмена мутаций — «из-копии»), кроме
установщика: он живёт только в пакете, дверь HARNESS_USTANOVIT.

Контракт кода (имена — для кодера шага Г):
  harness.conf: CHECKS_CPU_QUOTA, CHECKS_MEMORY_HIGH, CHECKS_MEMORY_MAX
  process_library.py checks-units <каталог> <проект> <журналы> <пользователь>
      пишет harness-checks.slice и пары юнитов в каталог; потолки — из файла
      $HARNESS_CONF, библиотека — через свои двери.

Прогон (только фоном):
    python3 scripts/test_checks_service.py
"""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RB = HERE / "run-background.sh"
PL = HERE / "lib" / "process_library.py"
SIGN = "служба-проверок"
SLICE = "harness-checks.slice"
UNIT_DIR = "/etc/systemd/system"
CEILING_KEYS = ("CHECKS_CPU_QUOTA", "CHECKS_MEMORY_HIGH", "CHECKS_MEMORY_MAX")
CEILING_UNIT = dict(zip(CEILING_KEYS, ("CPUQuota", "MemoryHigh", "MemoryMax")))
# Два набора потолков: число, зашитое в генератор, совпадёт не больше чем с одним.
CEILINGS_A = {"CHECKS_CPU_QUOTA": "150%", "CHECKS_MEMORY_HIGH": "1536M", "CHECKS_MEMORY_MAX": "1792M"}
CEILINGS_B = {"CHECKS_CPU_QUOTA": "175%", "CHECKS_MEMORY_HIGH": "1G", "CHECKS_MEMORY_MAX": "1280M"}
SUDO_VERBS = ("enable", "disable", "start", "stop")
# Форма живой строки 50-agent (переключение 06.11): sudo сверяет доводы буквально,
# поэтому «enable --now» — отдельное правило, а не следствие «enable».
SUDO_ACTIONS = SUDO_VERBS + ("enable --now", "disable --now")
# Формы, которых нет в живой библиотеке: перевод, верный на ней, на них ломался.
CALENDAR_FORMS = ("0 0 */2 * *", "0 0 1 */3 *", "0 9 * * 0-6", "0 9 * * 0-2",
                  "0 9 * * 6,0", "0 9 * * 7", "0 9 * * 5-7", "1-23/2 * * * *")
CALENDAR_REJECTED = ("0 9 1 * 1", "0 9 * * */2")
MIN_PYTHON = "python3.10"
MIN_PYTHON_FILES = ("lib/process_library.py", "build-system-jobs.py", "install-new.py")
BASE_TIME = datetime(2026, 11, 2, 0, 0)    # понедельник: недельное расписание тоже попадает в окно
ITERATIONS = 6


def config_dir() -> Path:
    # Раскладка проекта и пакета разные: конфиги лежат там, где config/system-jobs.yaml.
    for folder in HERE.parents:
        if (folder / "config" / "system-jobs.yaml").is_file():
            return folder / "config"
    return HERE.parent / "harness" / "config"


CONFIG = config_dir()
DEMONS = CONFIG.parent / "demons"
YAML_PATH = Path(os.environ.get("HARNESS_PROCESS_LIBRARY") or CONFIG / "process-library.yaml")
JSON_PATH = Path(os.environ.get("HARNESS_PROCESS_LIBRARY_JSON") or CONFIG / "process-library.json")
JOBS_PATH = CONFIG / "system-jobs.yaml"

paths = failures = 0


def report(ok: bool, text: str, bad=()) -> None:
    global paths, failures
    paths += 1
    if ok:
        print("ок    · " + text)
        return
    failures += 1
    bad = [str(b) for b in bad]
    tail = f": {', '.join(bad[:10])}" + (f" … ещё {len(bad) - 10}" if len(bad) > 10 else "") if bad else ""
    print("НЕ ок · " + text + tail)


def skip(text: str) -> None:
    print("ПРОПУСК · " + text)


# ── разбор крон-строки: ближайшие срабатывания ─────────────────────────────

def cron_field(spec: str, low: int, high: int) -> set:
    values = set()
    for part in spec.split(","):
        body, _, step = part.partition("/")
        if body == "*":
            first, last = low, high
        elif "-" in body:
            first, last = map(int, body.split("-"))
        else:
            first = last = int(body)
            if step:
                last = high
        values.update(range(first, last + 1, int(step or 1)))
    return values


def cron_next(expr: str, base: datetime, count: int, horizon_days: int = 0) -> list:
    minute, hour, dom, month, dow = expr.split()
    sets = (cron_field(minute, 0, 59), cron_field(hour, 0, 23), cron_field(dom, 1, 31),
            cron_field(month, 1, 12), {d % 7 for d in cron_field(dow, 0, 7)})
    # Правило крона: при заданных обоих днях срабатывает любое из них.
    either = dom != "*" and dow != "*"
    found, moment = [], base.replace(second=0, microsecond=0)
    for _ in range(60 * 24 * (horizon_days or 7 * (count + 1))):
        moment += timedelta(minutes=1)
        by_dom = moment.day in sets[2]
        by_dow = (moment.isoweekday() % 7) in sets[4]
        day_ok = (by_dom or by_dow) if either else (by_dom and by_dow)
        if moment.minute in sets[0] and moment.hour in sets[1] and moment.month in sets[3] and day_ok:
            found.append(moment)
            if len(found) == count:
                break
    return found


def calendar_next(values: list, base: datetime, count: int):
    """Срабатывания OnCalendar по systemd-analyze; None — инструмента нет."""
    if not shutil.which("systemd-analyze"):
        return None
    found = set()
    for value in values:
        done = subprocess.run(
            ["systemd-analyze", "calendar", f"--iterations={count}",
             f"--base-time={base:%Y-%m-%d %H:%M:%S}", value],
            capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
        if done.returncode != 0:
            return [f"systemd-analyze calendar не принял «{value}»: {done.stderr.strip()[:120]}"]
        for stamp in re.findall(r"(?:Next elapse|Iteration #\d+):\s+\w+ (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)",
                                done.stdout):
            found.add(datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    return sorted(found)[:count]


# ── файлы юнитов ───────────────────────────────────────────────────────────

def unit_keys(text: str) -> dict:
    """{ключ: [значения]} по всему файлу юнита."""
    keys = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ";", "[")) and "=" in line:
            key, _, value = line.partition("=")
            keys.setdefault(key.strip(), []).append(value.strip())
    return keys


def sign_ids(records: list) -> list:
    return [r.get("id") for r in records if isinstance(r, dict) and r.get("запуск") == SIGN]


def read_library(yaml_path: Path, json_path: Path):
    library_yaml = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    library_json = json.loads(json_path.read_text(encoding="utf-8"))
    return library_yaml, library_json


def write_conf(path: Path, ceilings: dict, background: str = "off") -> None:
    body = "".join(f'{k}="{v}"\n' for k, v in ceilings.items())
    path.write_text(f"BACKGROUND_PROCESSES={background}\n{body}", encoding="utf-8")


def stand_env(stand: Path, yaml_path: Path, json_path: Path, conf: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("HARNESS_TASK", "HARNESS_LOG_DIR", "HARNESS_EVENTS_STATE", "INSTALL_CONF",
                        "CLAUDE_CODE_SESSION_ID", "PROJECT_DIR", "BACKGROUND_PROCESSES")}
    env.update(HARNESS_HOME=str(stand / "home"), HARNESS_INSTALL_CONF=str(stand / "install.conf"),
               HARNESS_CONF=str(conf), LOG_DIR=str(stand / "log"),
               HARNESS_PROCESS_LIBRARY=str(yaml_path), HARNESS_PROCESS_LIBRARY_JSON=str(json_path),
               PYTHONDONTWRITEBYTECODE="1",
               HARNESS_PROCESS_LIBRARY_LOCAL="/nonexistent/harness-probe/process-library.local.json")
    return env


def generate_units(stand: Path, env: dict, out: Path, project: Path, logs: Path, user: str):
    out.mkdir(parents=True, exist_ok=True)
    done = subprocess.run([sys.executable, "-I", str(PL), "checks-units", str(out), str(project),
                           str(logs), user], capture_output=True, text=True, timeout=60, env=env)
    return done


def judge_units(label: str, out: Path, ids: list, library_yaml: dict, records: dict,
                ceilings: dict, project: Path, logs: Path, user: str, done) -> None:
    processes = library_yaml.get("процессы") or {}
    report(done.returncode == 0, f"{label}: генератор checks-units отработал",
           [f"rc={done.returncode}: {(done.stderr or done.stdout).strip()[:200]}"])
    made = {p.name: p.read_text(encoding="utf-8") for p in out.glob("*") if p.is_file()}
    made_ids = sorted(n[len("harness-"):-len(".service")] for n in made if n.endswith(".service"))
    report(bool(ids) and made_ids == sorted(ids), f"{label}: службы ровно у id с признаком ({len(ids)})",
           [f"лишние {sorted(set(made_ids) - set(ids))}, нет {sorted(set(ids) - set(made_ids))}"]
           if made_ids != sorted(ids) else ["id с признаком не найдено"])

    print(f"── {label}: 1. service+timer, срез, OnCalendar ──")
    no_pair, bad_service, bad_timer, bad_calendar = [], [], [], []
    for pid in ids:
        service, timer = made.get(f"harness-{pid}.service"), made.get(f"harness-{pid}.timer")
        if service is None or timer is None:
            no_pair.append(pid)
            continue
        entry = processes.get(pid) or {}
        s = unit_keys(service)
        wrapper = f"{project}/scripts/run-background.sh {pid} -- {project}/{entry.get('команда')}"
        want = {"Type": "oneshot", "User": user, "Slice": SLICE}
        bad_service += [f"{pid}: {k}={s.get(k)}" for k, v in want.items() if s.get(k) != [v]]
        if not any(wrapper in v for v in s.get("ExecStart", [])):
            bad_service.append(f"{pid}: ExecStart не через run-background.sh {pid} (ждали «{wrapper}»)")
        if not any(v == f"append:{logs}/{entry.get('журнал')}" for v in s.get("StandardOutput", [])):
            bad_service.append(f"{pid}: StandardOutput не append:{logs}/{entry.get('журнал')}")
        t = unit_keys(timer)
        if t.get("Persistent") != ["false"]:
            bad_timer.append(f"{pid}: Persistent={t.get('Persistent')}")
        if t.get("WantedBy") != ["timers.target"]:
            bad_timer.append(f"{pid}: WantedBy={t.get('WantedBy')}")
        calendar = t.get("OnCalendar") or []
        cron = entry.get("расписание")
        if not calendar or not cron:
            bad_calendar.append(f"{pid}: OnCalendar={calendar}, расписание={cron!r}")
            continue
        theirs = calendar_next(calendar, BASE_TIME, ITERATIONS)
        if theirs is None:
            skip(f"{label}: {pid} — systemd-analyze нет, OnCalendar не сверен по срабатываниям")
            continue
        mine = cron_next(cron, BASE_TIME, ITERATIONS)
        if theirs != mine:
            bad_calendar.append(f"{pid}: «{cron}» → {calendar}: "
                                f"ждали {[f'{m:%a %d %H:%M}' for m in mine[:3]]}, "
                                f"таймер {[f'{m:%a %d %H:%M}' if isinstance(m, datetime) else m for m in theirs[:3]]}")
    report(bool(ids) and not no_pair, f"{label}: у каждого id есть service и timer", no_pair)
    report(bool(ids) and not no_pair and not bad_service,
           f"{label}: служба — Type=oneshot, User, Slice={SLICE}, ExecStart через обёртку, журнал задания",
           bad_service)
    report(bool(ids) and not no_pair and not bad_timer, f"{label}: таймер — Persistent=false, WantedBy=timers.target",
           bad_timer)
    report(bool(ids) and not no_pair and not bad_calendar,
           f"{label}: OnCalendar равен расписанию библиотеки поштучно (по {ITERATIONS} срабатываниям)", bad_calendar)

    print(f"── {label}: 2. потолки среза и verify ──")
    slice_text = made.get(SLICE)
    s = unit_keys(slice_text or "")
    wrong = [f"{CEILING_UNIT[k]}={s.get(CEILING_UNIT[k])} (ждали {v})" for k, v in ceilings.items()
             if s.get(CEILING_UNIT[k]) != [v]]
    report(slice_text is not None and not wrong, f"{label}: {SLICE} — CPUQuota/MemoryHigh/MemoryMax из конфига",
           wrong or ([f"файла {SLICE} нет"] if slice_text is None else ()))
    files = [out / n for n in sorted(made)]
    if not shutil.which("systemd-analyze"):
        skip(f"{label}: systemd-analyze нет — verify юнитов не прогнан")
    else:
        verify = subprocess.run(["systemd-analyze", "verify", *map(str, files)],
                                capture_output=True, text=True, timeout=120)
        ours = [line for line in (verify.stdout + verify.stderr).splitlines() if str(out) in line]
        report(bool(files) and slice_text is not None and not ours,
               f"{label}: systemd-analyze verify без замечаний на {len(files)} файлах",
               ours or (["юнитов нет"] if not files or slice_text is None else ()))


def cron_ids(lines: list) -> set:
    return {m.group(1) for line in lines for m in [re.search(r"run-background\.sh (\S+) -- ", line)] if m}


def judge_cron(label: str, env: dict, ids: list, project: Path, logs: Path) -> None:
    print(f"── {label}: 3. cron_lines без id с признаком ──")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import process_library as p; "
            "print('\\n'.join(p.cron_lines(sys.argv[2], sys.argv[3])))")
    done = subprocess.run([sys.executable, "-I", "-c", code, str(PL.parent), str(project), str(logs)],
                          capture_output=True, text=True, timeout=60, env=env)
    by_function = cron_ids(done.stdout.splitlines())
    leaked = sorted(by_function & set(ids))
    report(bool(ids) and done.returncode == 0 and bool(by_function) and not leaked,
           f"{label}: cron_lines() не выдаёт id с признаком, прочие выдаёт ({len(by_function)})",
           leaked or [f"rc={done.returncode} {done.stderr.strip()[:160]}" if done.returncode
                      else "строк нет" if not by_function else "id с признаком не найдено"])
    done = subprocess.run([sys.executable, "-I", str(PL), "cron-lines", str(JOBS_PATH), str(project), str(logs)],
                          capture_output=True, text=True, timeout=60, env=env)
    by_cli = cron_ids(done.stdout.splitlines())
    leaked = sorted(by_cli & set(ids))
    report(bool(ids) and done.returncode == 0 and bool(by_cli) and not leaked,
           f"{label}: CLI cron-lines (установщик) не выдаёт id с признаком ({len(by_cli)})",
           leaked or [f"rc={done.returncode} {done.stderr.strip()[:160]}" if done.returncode
                      else "строк нет" if not by_cli else "id с признаком не найдено"])


# ── обёртка при off ────────────────────────────────────────────────────────

def journal_rows(path: Path) -> list:
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def live_log_dir() -> Path:
    env = {k: v for k, v in os.environ.items() if k not in ("LOG_DIR", "HARNESS_HOME")}
    done = subprocess.run(["bash", "-c", 'source "$1"; konf_zagruzit; printf %s "$LOG_DIR"', "_",
                           str(HERE / "lib" / "config.sh")], capture_output=True, text=True, timeout=30, env=env)
    return Path(done.stdout.strip() or "/var/log/harness")


def judge_wrapper(stand: Path, base_yaml: dict, base_json: dict) -> None:
    print("── 4. run-background.sh при off ──")
    signed, plain = f"probe-checks-{os.getpid()}", f"probe-plain-{os.getpid()}"
    library_json = json.loads(json.dumps(base_json))
    library_json["процессы"] = list(library_json.get("процессы") or []) + [
        {"id": signed, "класс": "фоновый", "запуск": SIGN},
        {"id": plain, "класс": "фоновый", "запуск": "крон"}]
    json_path, yaml_path = stand / "w-library.json", stand / "w-library.yaml"
    json_path.write_text(json.dumps(library_json, ensure_ascii=False), encoding="utf-8")
    yaml_path.write_text(yaml.safe_dump(base_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8")
    live_journal = live_log_dir() / "process-runs.jsonl"

    for number, (conf_state, conf_body) in enumerate((("off", "BACKGROUND_PROCESSES=off\n"), ("конфига нет", None))):
        run = stand / f"w-{number}"
        (run / "log").mkdir(parents=True)
        (run / "home").mkdir()
        (run / "install.conf").write_text("", encoding="utf-8")
        conf = run / "harness.conf"
        if conf_body is not None:
            conf.write_text(conf_body, encoding="utf-8")
        env = stand_env(run, yaml_path, json_path, conf)
        journal = run / "log" / "process-runs.jsonl"
        for pid, signed_case in ((signed, True), (plain, False)):
            if not signed_case and conf_body is None:
                continue
            trace = run / f"trace-{pid}"
            done = subprocess.run(["bash", str(RB), pid, "--", "bash", "-c", 'echo ran > "$1"', "_", str(trace)],
                                  capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
            events = [r.get("event") for r in journal_rows(journal) if (r.get("data") or {}).get("id") == pid]
            got = f"rc={done.returncode}, след={'есть' if trace.exists() else 'нет'}, события={events}"
            if signed_case:
                report(trace.exists() and events == ["process.start", "process.end"] and done.returncode == 0,
                       f"4 {conf_state}: запись с признаком идёт, process.start/end, без skip", [got])
            else:
                report(not trace.exists() and events == ["process.skip"],
                       f"4 {conf_state}: запись без признака — process.skip, следа нет", [got])
    leaked = [r for r in journal_rows(live_journal) if (r.get("data") or {}).get("id") in (signed, plain)]
    report(not leaked, f"4 живой {live_journal} не получил записей стенда", [f"записей {len(leaked)}"])


# ── установщик --dry-run ───────────────────────────────────────────────────

def find_installer():
    door = os.environ.get("HARNESS_USTANOVIT")
    candidates = [folder / "USTANOVIT.sh" for folder in HERE.parents]
    home = Path(f"/home/{getpass.getuser()}")
    candidates += [home / "starter" / "STARTER-PACKAGE" / "USTANOVIT.sh", home / "STARTER-PACKAGE" / "USTANOVIT.sh"]
    package = next((c.parent for c in candidates if c.is_file() and (c.parent / "harness").is_dir()), None)
    installer = Path(door) if door else (package / "USTANOVIT.sh" if package else None)
    return installer, package


def parse_dry_run(text: str):
    """(записанные файлы {путь: содержимое}, [(номер, команда)], номер строки записи каждого файла)."""
    files, order, commands, current = {}, {}, [], None
    for number, line in enumerate(text.splitlines()):
        m = re.match(r"^  \+ записать файл (\S+) \(", line)
        if m:
            current = m.group(1)
            files[current] = ""
            order[current] = number
            continue
        if current and line.startswith("    | "):
            files[current] += line[6:] + "\n"
            continue
        current = None
        if line.startswith("  + "):
            commands.append((number, line[4:]))
    return files, commands, order


def judge_installer(stand: Path, yaml_path: Path, json_path: Path, ids: list, ceilings: dict) -> None:
    print("── 5. USTANOVIT.sh --dry-run ──")
    installer, package = find_installer()
    if not installer or not installer.is_file() or not package:
        report(False, "5 USTANOVIT.sh найден (HARNESS_USTANOVIT, каталоги над пробой, дом агента)",
               [f"установщик={installer}, пакет={package}"])
        return
    # Копия установщика среди соседей пакета: мутированная копия зовётся так же, как боевой.
    pkg = stand / "pkg"
    pkg.mkdir()
    shutil.copy2(installer, pkg / "USTANOVIT.sh")
    for entry in package.iterdir():
        if entry.name != "USTANOVIT.sh":
            (pkg / entry.name).symlink_to(entry)
    conf = stand / "u-harness.conf"
    write_conf(conf, ceilings)
    env = stand_env(stand, yaml_path, json_path, conf)
    env["HARNESS_INSTALL_CONF"] = str(stand / "u-нет-install.conf")
    done = subprocess.run(["bash", str(pkg / "USTANOVIT.sh"), "--dry-run"], capture_output=True, text=True,
                          timeout=300, env=env, stdin=subprocess.DEVNULL, cwd=str(stand))
    report(done.returncode == 0, "5 --dry-run вышел с кодом 0", [f"rc={done.returncode}: {done.stderr.strip()[-200:]}"])
    files, commands, order = parse_dry_run(done.stdout)
    want = [f"{UNIT_DIR}/{SLICE}"] + [f"{UNIT_DIR}/harness-{pid}.{kind}" for pid in ids for kind in ("service", "timer")]
    missing = [p for p in want if p not in files]
    report(bool(ids) and not missing, f"5 пишет срез и юниты: {len(want)} файлов (1 + 2 × {len(ids)})",
           missing or ["id с признаком не найдено"])
    s = unit_keys(files.get(f"{UNIT_DIR}/{SLICE}", ""))
    wrong = [f"{CEILING_UNIT[k]}={s.get(CEILING_UNIT[k])} (ждали {v})" for k, v in ceilings.items()
             if s.get(CEILING_UNIT[k]) != [v]]
    report(f"{UNIT_DIR}/{SLICE}" in files and not wrong, "5 потолки среза — из $HARNESS_CONF", wrong or ["среза нет"])
    verified = {m.group(1) for _, c in commands for m in [re.match(r"systemd-analyze verify (\S+)$", c)] if m}
    report(bool(ids) and not missing and set(want) <= verified, "5 каждый юнит выверен (проверить_юнит)",
           sorted(set(want) - verified))
    sudoers = files.get("/etc/sudoers.d/50-agent", "")
    narrow = [f"systemctl {verb} harness-{pid}.timer" for pid in ids for verb in SUDO_VERBS]
    narrow.append("systemctl daemon-reload")
    absent = [rule for rule in narrow if not re.search(r"/usr/bin/" + re.escape(rule) + r"(\s*,|\s*$)", sudoers, re.M)]
    report(bool(ids) and bool(sudoers) and not absent,
           f"5 узкая строка 50-agent: {'/'.join(SUDO_VERBS)} таймеров и daemon-reload ({len(narrow)} правил)",
           absent or ["id с признаком не найдено"])
    checks_line = next((line for line in sudoers.splitlines() if "systemctl daemon-reload" in line), "")
    granted = {c.strip() for c in checks_line.partition("NOPASSWD:")[2].split(",") if c.strip()}  # не-секрет
    exact = {"/usr/bin/systemctl daemon-reload"} | {f"/usr/bin/systemctl {action} harness-{pid}.timer"
                                                    for pid in ids for action in SUDO_ACTIONS}
    report(bool(ids) and bool(checks_line) and "*" not in checks_line and granted == exact,
           f"5 строка службы в 50-agent = daemon-reload + {len(SUDO_ACTIONS)} форм на таймер "
           f"(с enable --now/disable --now), без «*» ({len(exact)} правил)",
           [f"нет {sorted(exact - granted)[:4]}", f"лишние {sorted(granted - exact)[:4]}",
            f"«*» {'есть' if '*' in checks_line else 'нет'}"] if checks_line else ["строки с daemon-reload нет"])
    manual = re.search(r"^=+ ОСТАЛОСЬ РУКАМИ.*?$(.*?)(?=^=+ |\Z)", done.stdout, re.M | re.S)
    manual = manual.group(1) if manual else ""
    enabled = " ".join(c for _, c in commands if c.startswith("включить_таймеры"))
    missing_timers = [f"harness-{pid}.timer" for pid in ids if f"harness-{pid}.timer" not in enabled]
    report(bool(ids) and bool(enabled) and not missing_timers,
           "5 установщик сам включает таймер каждой проверки (enable --now), без ручного шага владельцу",
           missing_timers or ["вызова включить_таймеры нет"])
    report("все_таймеры_включены" in done.stdout and not re.search(r"harness-\S+\.timer", manual),
           "5 проба «все таймеры включены» есть, в «ОСТАЛОСЬ РУКАМИ» таймеров не осталось",
           ["пробы нет" if "все_таймеры_включены" not in done.stdout else "таймер остался руками"])
    last_unit = max((order[p] for p in want if p in order), default=None)
    reload_after = [n for n, c in commands if "systemctl daemon-reload" in c and last_unit is not None and n > last_unit]
    report(bool(ids) and not missing and bool(reload_after), "5 daemon-reload после записи юнитов",
           ["нет daemon-reload после последнего юнита"])


# ── перевод календаря вне живой библиотеки ────────────────────────────────

def judge_calendar_forms(env: dict) -> None:
    print("── 6. перевод календаря: формы вне живой библиотеки ──")
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import process_library as p\n"
            "out = {}\n"
            "for cron in sys.argv[2:]:\n"
            "    try:\n        out[cron] = ['ok', p.on_calendar(cron)]\n"
            "    except Exception as error:\n        out[cron] = [type(error).__name__, str(error)]\n"
            "print(json.dumps(out, ensure_ascii=False))")
    done = subprocess.run([sys.executable, "-I", "-c", code, str(PL.parent), *CALENDAR_FORMS, *CALENDAR_REJECTED],
                          capture_output=True, text=True, timeout=60, env=env)
    try:
        verdicts = json.loads(done.stdout)
    except ValueError:
        report(False, "6 on_calendar зовётся", [f"rc={done.returncode}: {done.stderr.strip()[-200:]}"])
        return
    for cron in CALENDAR_FORMS:
        kind, value = verdicts.get(cron, ["нет ответа", ""])
        if kind != "ok":
            report(False, f"6 «{cron}» переводится", [f"{kind}: {value}"])
            continue
        theirs = calendar_next([value], BASE_TIME, 2)
        if theirs is None:
            skip(f"6 «{cron}» → «{value}»: systemd-analyze нет, перевод не сверен")
            continue
        mine = cron_next(cron, BASE_TIME, 2, horizon_days=200)
        shown = [f"{m:%a %Y-%m-%d %H:%M}" if isinstance(m, datetime) else m for m in theirs]
        report(len(mine) == 2 and theirs == mine,
               f"6 «{cron}» → «{value}»: systemd принимает, два срабатывания как у крона",
               [f"крон {[f'{m:%a %Y-%m-%d %H:%M}' for m in mine]}, таймер {shown}"])
    for cron in CALENDAR_REJECTED:
        kind, value = verdicts.get(cron, ["нет ответа", ""])
        report(kind == "ValueError", f"6 «{cron}» не переводится — ValueError", [f"{kind}: {value}"])


# ── синтаксис под минимальной версией Python ───────────────────────────────

def _quote(token: str) -> str:
    body = token.lstrip("rRbBuUfF")
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def same_quote_in_fstring(source: str) -> list:
    """[(строка, токен)] — строка внутри {…} f-строки с кавычкой, которая до 3.12
    закрыла бы саму f-строку. ast этого не видит: разбор идёт парсером 3.12."""
    import io
    import tokenize
    found, stack = [], []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in (tokenize.STRING, tokenize.FSTRING_START, tokenize.FSTRING_END):
            continue
        if token.type == tokenize.FSTRING_END:
            stack.pop()
            continue
        quote = _quote(token.string)
        if any(quote == outer if len(outer) == 3 else quote.startswith(outer) for outer in stack):
            found.append((token.start[0], token.string))
        if token.type == tokenize.FSTRING_START:
            stack.append(quote)
    return found


def judge_min_python() -> None:
    print(f"── 7. синтаксис под {MIN_PYTHON} (Ubuntu 22.04) ──")
    files = [HERE / name for name in MIN_PYTHON_FILES]
    absent = [str(f) for f in files if not f.is_file()]
    report(not absent, f"7 файлы для проверки на месте ({len(files)})", absent)
    files = [f for f in files if f.is_file()]
    old = shutil.which(MIN_PYTHON)
    if old:
        check = "import sys; compile(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1], 'exec')"
        for path in files:
            done = subprocess.run([old, "-I", "-c", check, str(path)], capture_output=True, text=True, timeout=60)
            report(done.returncode == 0, f"7 {MIN_PYTHON} компилирует {path.name}", [done.stderr.strip()[-200:]])
    else:
        skip(f"7 {MIN_PYTHON} на машине нет — судит разбор токенов")
    if sys.version_info < (3, 12):
        for path in files:
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
                report(True, f"7 {path.name} компилируется на {sys.version.split()[0]}")
            except SyntaxError as error:
                report(False, f"7 {path.name} компилируется на {sys.version.split()[0]}", [error])
        return
    # Сам детектор: законная форма не ловится, запрещённая ловится — иначе зелёное даром.
    legal = "x = f\"{d['a']}\" + f'''{d[\"b\"]}'''\n"
    banned = "x = f\"{d[\"a\"]}\"\n"
    sane = not same_quote_in_fstring(legal) and len(same_quote_in_fstring(banned)) == 1
    report(sane, "7 детектор кавычек: законное не ловит, запрещённое до 3.12 ловит",
           [f"законное {same_quote_in_fstring(legal)}, запрещённое {same_quote_in_fstring(banned)}"])
    for path in files:
        found = same_quote_in_fstring(path.read_text(encoding="utf-8"))
        report(sane and not found, f"7 {path.name}: в f-строках нет той же кавычки внутри {{…}}",
               [f"{path.name}:{line} {text}" for line, text in found])


def main() -> int:
    stand = Path(tempfile.mkdtemp(prefix="test-checks-service-"))
    try:
        print("── 0. разбор библиотеки ──")
        try:
            base_yaml, base_json = read_library(YAML_PATH, JSON_PATH)
        except (OSError, ValueError, yaml.YAMLError) as error:
            report(False, "библиотека читается", [error])
            return 1
        records = {r.get("id"): r for r in base_json.get("процессы") or [] if isinstance(r, dict)}
        processes = base_yaml.get("процессы") or {}
        ids = sign_ids(base_json.get("процессы") or [])
        report(bool(ids), f"id с признаком «запуск: {SIGN}» в process-library.json: {len(ids)}",
               ["разбор пуст"] if not ids else ())
        no_schedule = [pid for pid in ids if not (processes.get(pid) or {}).get("расписание")]
        report(bool(ids) and not no_schedule, "у каждого id с признаком есть расписание в process-library.yaml",
               no_schedule)

        user = getpass.getuser()
        project = stand / "proj"
        (project / "harness").mkdir(parents=True)
        (project / "scripts").symlink_to(HERE)
        (project / "harness" / "demons").symlink_to(DEMONS)
        logs = stand / "logs"
        (stand / "home").mkdir()
        (stand / "log").mkdir()
        (stand / "install.conf").write_text("", encoding="utf-8")

        # Проход А: живые расписания и признаки.
        conf_a = stand / "a-harness.conf"
        write_conf(conf_a, CEILINGS_A)
        yaml_a, json_a = stand / "a-library.yaml", stand / "a-library.json"
        shutil.copy2(YAML_PATH, yaml_a)
        shutil.copy2(JSON_PATH, json_a)
        env_a = stand_env(stand, yaml_a, json_a, conf_a)
        done = generate_units(stand, env_a, stand / "units-a", project, logs, user)
        judge_units("А", stand / "units-a", ids, base_yaml, records, CEILINGS_A, project, logs, user, done)
        judge_cron("А", env_a, ids, project, logs)

        # Проход Б: другие расписания, признак у чужой записи, другие потолки.
        extra = next((name for name, entry in processes.items()
                      if isinstance(entry, dict) and entry.get("расписание") and name not in ids
                      and name in records), None)
        ids_b = ids + ([extra] if extra else [])
        yaml_b = json.loads(json.dumps(base_yaml))
        for number, pid in enumerate(ids_b):
            yaml_b["процессы"][pid]["расписание"] = f"{(number * 7 + 3) % 60} {(number * 5 + 1) % 24} * * {number % 7}"
        json_b = json.loads(json.dumps(base_json))
        for record in json_b.get("процессы") or []:
            if isinstance(record, dict) and record.get("id") == extra:
                record["запуск"] = SIGN
        path_yb, path_jb = stand / "b-library.yaml", stand / "b-library.json"
        path_yb.write_text(yaml.safe_dump(yaml_b, allow_unicode=True, sort_keys=False), encoding="utf-8")
        path_jb.write_text(json.dumps(json_b, ensure_ascii=False), encoding="utf-8")
        conf_b = stand / "b-harness.conf"
        write_conf(conf_b, CEILINGS_B)
        env_b = stand_env(stand, path_yb, path_jb, conf_b)
        print(f"── проход Б: расписания стенда, признак ещё у «{extra}», потолки {CEILINGS_B} ──")
        b_ids = ids_b if ids else []      # без живых признаков проход Б не доказывает ничего
        done = generate_units(stand, env_b, stand / "units-b", project, logs, user)
        judge_units("Б", stand / "units-b", b_ids, yaml_b, records, CEILINGS_B, project, logs, user, done)
        judge_cron("Б", env_b, b_ids, project, logs)

        judge_wrapper(stand, base_yaml, base_json)
        judge_installer(stand, path_yb, path_jb, b_ids, CEILINGS_B)
        judge_calendar_forms(env_a)
        judge_min_python()
    finally:
        shutil.rmtree(stand, ignore_errors=True)
    print(f"=== служба потока проверок: путей {paths}, неудач {failures} ===")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
