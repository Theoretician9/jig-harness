#!/usr/bin/env python3
"""Пробы скрипта строки состояния `scripts/statusline_limits.py` (Ч9 hr.speed-modes).

Критерий — spec.md 10.3 п. 17; поля — `.tasks/hr.speed-modes/ч9-поля-rate-limits.md`.
Claude Code зовёт скрипт на каждом обновлении интерфейса и кладёт в stdin JSON сессии;
в нём `rate_limits.five_hour` и `rate_limits.seven_day` ({used_percentage 0–100, resets_at
unix-секунды}) — только у подписчиков, только после первого ответа API, окно пропадает
после своего сброса. Скрипт печатает одну короткую строку и атомарно пишет файл лимитов,
который читает адаптер `claude` судьи скорости (`read_limit_file` в
harness/demons/speed_mode.py). Ошибка скрипта — сломанная строка интерфейса либо
файл, по которому судья снизит режим на чужом или испорченном проценте.

Формат файла — тот, что читает адаптер: `{"ts": …, "rate_limits": {"five_hour": {…},
"seven_day": {…}}}`; пропавшее окно — ключа нет или null.

Каждый случай строит свой стенд во временном каталоге: свой каталог состояния панели
(HARNESS_PANEL_STATE, группа и setgid как у живого), свой файл лимитов. Живой файл лимитов
(`лимит_подписки.файл_claude`) только сверяется: изменился за прогон — красный.

Двери: STATUSLINE_LIMITS_BIN — скрипт (копия под мутацией), SPEED_MODE_BIN — судья
с адаптером.

Прогон (в фоне, по правилам проекта):
    python3 scripts/test_statusline_limits.py [--случай имя]
Последняя строка: «СТРОКА-ЛИМИТЫ: проб N, проверок K, провалов M»; код 0 — все зелёные.
"""
from __future__ import annotations

import datetime as dt
import grp
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def tree_file(*parts: str) -> Path:
    # Проект: scripts/ рядом с harness/; пакет: harness/scripts/ рядом с demons/ и config/.
    for candidate in (HERE.parent / "harness" / Path(*parts), HERE.parent / Path(*parts)):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / Path(*parts)


SCRIPT = Path(os.environ.get("STATUSLINE_LIMITS_BIN") or HERE / "statusline_limits.py")
ADAPTER = Path(os.environ.get("SPEED_MODE_BIN") or tree_file("demons", "speed_mode.py"))
SPEED_DATA = tree_file("config", "speed-modes.yaml")
FILE_NAME = "лимиты-подписки.json"
PANEL_GROUP = "harness-panel"
TIMEOUT = 10
WINDOW_KEYS = ("five_hour", "seven_day")

# Строка интерфейса: одна, короткая, без хвоста трассировки.
LINE_MAX = 80
# Скорость меряется против голого запуска интерпретатора в те же минуты: при нагрузке
# медленнее оба, а разница — то, что добавил сам скрипт (правило про нагрузку).
TIMING_RUNS = 100
OWN_MEDIAN_MAX_MS = 150
P95_FACTOR = 3
SINGLE_MAX_MS = 2000
ATOMIC_WRITES = 40

DRIVER = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("speed_mode", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps(mod.read_limit_file(json.loads(sys.argv[2]), float(sys.argv[3])), ensure_ascii=False))
'''


class ProbeError(Exception):
    """Стенд не смог прогнать скрипт — случай красный с этой причиной."""


def session(rate_limits=..., **extra) -> dict:
    """JSON сессии как у claude 2.1.299; rate_limits=... — ключа нет вовсе."""
    body = {"session_id": "проба-ч9", "version": "2.1.299",
            "model": {"id": "claude-opus", "display_name": "Opus"},
            "workspace": {"current_dir": "/tmp"},
            "context_window": {"used_percentage": 12}}
    if rate_limits is not ...:
        body["rate_limits"] = rate_limits
    body.update(extra)
    return body


def window(percent, reset) -> dict:
    return {"used_percentage": percent, "resets_at": reset}


class Stand:
    def __init__(self, root: Path):
        self.root = root
        self.panel = root / "panel-state"
        self.panel.mkdir()
        self.gid = self._panel_gid()
        os.chown(self.panel, -1, self.gid)
        os.chmod(self.panel, 0o2770)
        self.file = self.panel / FILE_NAME

    @staticmethod
    def _panel_gid() -> int:
        # Группа, отличная от основной: иначе «группа как у каталога» совпала бы сама собой.
        try:
            return grp.getgrnam(PANEL_GROUP).gr_gid
        except KeyError:
            others = [g for g in os.getgroups() if g != os.getgid()]
            return others[0] if others else os.getgid()

    def env(self, panel: Path | None = None) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
        env["HARNESS_PANEL_STATE"] = str(panel or self.panel)
        return env

    def run(self, stdin, panel: Path | None = None) -> dict:
        if not SCRIPT.is_file():
            raise ProbeError(f"нет скрипта {SCRIPT}")
        if isinstance(stdin, dict):
            stdin = json.dumps(stdin)
        if isinstance(stdin, str):
            stdin = stdin.encode()
        before = time.time()
        started = time.monotonic()
        done = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True,
                              env=self.env(panel), timeout=TIMEOUT, cwd=self.root,
                              preexec_fn=lambda: os.umask(0o002))
        return {"code": done.returncode, "out": done.stdout.decode(errors="replace"),
                "err": done.stderr.decode(errors="replace"),
                "ms": (time.monotonic() - started) * 1000, "before": before, "after": time.time()}

    def seed(self, content: dict) -> bytes:
        raw = json.dumps(content, ensure_ascii=False).encode()
        self.file.write_bytes(raw)
        os.chmod(self.file, 0o640)
        return raw


class Case:
    def __init__(self, name: str):
        self.name = name
        self.checks = 0
        self.failures: list[str] = []

    def check(self, ok: bool, what: str) -> bool:
        self.checks += 1
        if not ok:
            self.failures.append(what)
        return ok


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_ts(value):
    # Адаптер принимает ts числом или ISO-строкой — проба тоже.
    if finite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def ui_safe(case: Case, run: dict, label: str) -> str:
    """Общее для каждого вызова: код 0, одна короткая строка, stderr пуст."""
    case.check(run["code"] == 0, f"{label}: код {run['code']}, stderr {run['err'][-200:]!r}")
    lines = run["out"].strip().splitlines()
    case.check(len(lines) == 1 and lines[0].strip() != "",
               f"{label}: ждём одну непустую строку, stdout {run['out'][:200]!r}")
    case.check(len(run["out"].strip()) <= LINE_MAX, f"{label}: строка длиннее {LINE_MAX}: {run['out'][:200]!r}")
    case.check(run["err"] == "", f"{label}: мусор в stderr {run['err'][-200:]!r}")
    return lines[0] if lines else ""


def read_file(case: Case, stand: Stand, label: str) -> dict | None:
    try:
        data = json.loads(stand.file.read_bytes())
    except (OSError, ValueError) as error:
        case.check(False, f"{label}: файл лимитов не читается: {error}")
        return None
    case.check(isinstance(data, dict) and isinstance(data.get("rate_limits"), dict),
               f"{label}: в файле нет объекта rate_limits: {data!r}"[:300])
    return data if isinstance(data, dict) else None


def file_window(data, key: str):
    rates = data.get("rate_limits") if isinstance(data, dict) else None
    return rates.get(key) if isinstance(rates, dict) else None


def check_window(case: Case, data, key: str, expected: dict, label: str) -> None:
    got = file_window(data, key)
    case.check(isinstance(got, dict) and finite(got.get("used_percentage"))
               and got.get("used_percentage") == expected["used_percentage"]
               and finite(got.get("resets_at")) and got.get("resets_at") == expected["resets_at"],
               f"{label}: окно {key} в файле {got!r}, ждём {expected!r}")


def check_gone(case: Case, data, key: str, label: str) -> None:
    got = file_window(data, key)
    case.check(got is None, f"{label}: окна {key} в файле быть не должно, а там {got!r}")


def check_fresh(case: Case, data, run: dict, label: str) -> None:
    stamp = parse_ts((data or {}).get("ts"))
    case.check(stamp is not None and run["before"] - 1 <= stamp <= run["after"] + 1,
               f"{label}: ts {(data or {}).get('ts')!r} не из времени прогона")


def check_mode(case: Case, stand: Stand, label: str) -> None:
    st = stand.file.stat()
    case.check(st.st_mode & 0o777 == 0o640, f"{label}: права {oct(st.st_mode & 0o777)}, ждём 0o640")
    case.check(st.st_gid == stand.gid, f"{label}: группа {st.st_gid}, у каталога {stand.gid}")


def check_no_litter(case: Case, stand: Stand, label: str) -> None:
    extra = sorted(p.name for p in stand.panel.iterdir() if p.name != FILE_NAME)
    case.check(not extra, f"{label}: мусор в каталоге панели {extra}")


def mentions(line: str, percent) -> bool:
    return any(shown in line for shown in (f"{percent:g}", str(round(percent))))


# ───────────────────────── случаи ─────────────────────────

def case_both_windows(case: Case, stand: Stand) -> None:
    now = int(time.time())
    five, seven = window(42, now + 3600), window(18, now + 5 * 86400)
    run = stand.run(session({"five_hour": five, "seven_day": seven}))
    line = ui_safe(case, run, "оба окна")
    data = read_file(case, stand, "оба окна")
    check_window(case, data, "five_hour", five, "оба окна")
    check_window(case, data, "seven_day", seven, "оба окна")
    check_fresh(case, data, run, "оба окна")
    check_mode(case, stand, "оба окна")
    check_no_litter(case, stand, "оба окна")
    case.check(mentions(line, 42) and mentions(line, 18), f"оба окна: в строке нет 42 и 18: {line!r}")


def case_five_hour_only(case: Case, stand: Stand) -> None:
    now = int(time.time())
    five = window(7, now + 1800)
    run = stand.run(session({"five_hour": five}))
    line = ui_safe(case, run, "только 5ч")
    data = read_file(case, stand, "только 5ч")
    check_window(case, data, "five_hour", five, "только 5ч")
    check_gone(case, data, "seven_day", "только 5ч")
    check_fresh(case, data, run, "только 5ч")
    case.check(mentions(line, 7), f"только 5ч: в строке нет 7: {line!r}")


NO_LIMITS = (("ключа нет", session()), ("rate_limits null", session(None)))


def case_no_rate_limits(case: Case, stand: Stand) -> None:
    # Без прежнего файла писать нечего — файла нет и после.
    for label, body in NO_LIMITS:
        line = ui_safe(case, stand.run(body), f"{label}, файла не было")
        case.check("нет данных" in line, f"{label}: ждём «нет данных», строка {line!r}")
        case.check(not stand.file.exists(), f"{label}: файл создан без данных")
        check_no_litter(case, stand, label)
    # С прежним файлом: не затёрт ни байтом.
    now = int(time.time())
    before = stand.seed({"ts": now - 60, "rate_limits": {"five_hour": window(55, now + 600),
                                                         "seven_day": window(30, now + 86400)}})
    for label, body in NO_LIMITS:
        ui_safe(case, stand.run(body), f"{label}, файл был")
        case.check(stand.file.read_bytes() == before, f"{label}: прежний файл затёрт")


def case_window_gone_after_reset(case: Case, stand: Stand) -> None:
    now = int(time.time())
    stand.seed({"ts": now - 30, "rate_limits": {"five_hour": window(90, now - 5),
                                                "seven_day": window(60, now + 86400)}})
    seven = window(61.5, now + 86400)
    run = stand.run(session({"seven_day": seven}))
    ui_safe(case, run, "5ч сброшено")
    data = read_file(case, stand, "5ч сброшено")
    check_gone(case, data, "five_hour", "5ч сброшено")
    check_window(case, data, "seven_day", seven, "5ч сброшено")
    check_fresh(case, data, run, "5ч сброшено")
    # Снимок интерфейса может отстать от сброса: окно ещё пришло, а его resets_at уже прошёл.
    ui_safe(case, stand.run(session({"five_hour": window(90, now - 5), "seven_day": seven})), "сброс прошёл")
    check_gone(case, read_file(case, stand, "сброс прошёл"), "five_hour", "сброс прошёл")


BROKEN_STDIN = (
    ("пустой stdin", b""),
    ("пробелы", b"  \n"),
    ("не JSON", b"{\"rate_limits\": {\"five_hour\""),
    ("двоичный мусор", b"\xff\xfe\x00\x81"),
    ("массив", b"[1, 2]"),
    ("строка", b"\"rate_limits\""),
    ("rate_limits строкой", json.dumps(session("42%")).encode()),
)


def bad_windows(now: int) -> tuple:
    return (
        ("процент строкой", {"five_hour": window("abc", now + 600)}),
        ("процент null", {"five_hour": window(None, now + 600)}),
        ("процент true", {"five_hour": window(True, now + 600)}),
        ("процент NaN", {"five_hour": window(float("nan"), now + 600)}),
        ("окно строкой", {"five_hour": "42"}),
        ("сброс строкой", {"five_hour": window(42, "скоро")}),
    )


def file_sane(case: Case, stand: Stand, label: str) -> None:
    """Файл после плохого входа: читается, каждое окно — конечный процент, сброс число или null."""
    try:
        data = json.loads(stand.file.read_bytes())
    except (OSError, ValueError) as error:
        case.check(False, f"{label}: файл испорчен: {error}")
        return
    rates = data.get("rate_limits") if isinstance(data, dict) else None
    if not case.check(isinstance(rates, dict), f"{label}: в файле нет rate_limits: {data!r}"[:300]):
        return
    for key in WINDOW_KEYS:
        cell = rates.get(key)
        if cell is not None:
            case.check(isinstance(cell, dict) and finite(cell.get("used_percentage"))
                       and (cell.get("resets_at") is None or finite(cell.get("resets_at"))),
                       f"{label}: окно {key} в файле испорчено: {cell!r}")


def case_broken_input(case: Case, stand: Stand) -> None:
    now = int(time.time())
    seed = {"ts": now - 60, "rate_limits": {"five_hour": window(55, now + 600),
                                            "seven_day": window(30, now + 86400)}}
    for label, raw in BROKEN_STDIN:
        before = stand.seed(seed)
        line = ui_safe(case, stand.run(raw), label)
        case.check("нет данных" in line, f"{label}: ждём «нет данных», строка {line!r}")
        case.check(stand.file.read_bytes() == before, f"{label}: прежний файл затёрт")
        check_no_litter(case, stand, label)
    for label, rates in bad_windows(now):
        stand.seed(seed)
        line = ui_safe(case, stand.run(json.dumps(session(rates), allow_nan=True)), label)
        file_sane(case, stand, label)
        check_no_litter(case, stand, label)
        case.check("nan" not in line.lower() and "abc" not in line and "скоро" not in line,
                   f"{label}: мусор входа в строке {line!r}")


def case_dir_unwritable(case: Case, stand: Stand) -> None:
    now = int(time.time())
    body = session({"five_hour": window(42, now + 3600), "seven_day": window(18, now + 86400)})
    if not case.check(os.geteuid() != 0, "каталог без записи: прогон от root, chmod не запрещает запись"):
        return
    os.chmod(stand.panel, 0o2550)
    try:
        line = ui_safe(case, stand.run(body), "каталог только на чтение")
        case.check(mentions(line, 42), f"каталог только на чтение: в строке нет процента: {line!r}")
        case.check(not stand.file.exists(), "каталог только на чтение: файл всё же появился")
    finally:
        os.chmod(stand.panel, 0o2770)
    blocker = stand.root / "не-каталог"
    blocker.write_text("файл на месте каталога")
    line = ui_safe(case, stand.run(body, panel=blocker), "путь каталога — файл")
    case.check(mentions(line, 42), f"путь каталога — файл: в строке нет процента: {line!r}")
    # Замена падает уже после временного файла (цель — каталог): временный обязан уйти.
    stand.file.mkdir()
    ui_safe(case, stand.run(body), "цель — каталог")
    check_no_litter(case, stand, "цель — каталог")


def case_atomic(case: Case, stand: Stand) -> None:
    now = int(time.time())
    stand.seed({"ts": now, "rate_limits": {"five_hour": window(1, now + 600),
                                           "seven_day": window(1, now + 86400)}})
    seen = {"reads": 0, "bad": []}
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                data = json.loads(stand.file.read_bytes())
                ok = all(isinstance(file_window(data, k), dict) for k in WINDOW_KEYS)
            except (OSError, ValueError) as error:
                ok, data = False, repr(error)
            seen["reads"] += 1
            if not ok and len(seen["bad"]) < 3:
                seen["bad"].append(str(data)[:120])

    thread = threading.Thread(target=reader)
    thread.start()
    replaced = 0
    try:
        for i in range(ATOMIC_WRITES):
            inode = stand.file.stat().st_ino
            run = stand.run(session({"five_hour": window(i, now + 600), "seven_day": window(i, now + 86400)}))
            replaced += run["code"] == 0 and stand.file.stat().st_ino != inode
    finally:
        stop.set()
        thread.join()
    case.check(seen["reads"] > 0, "атомарность: читатель не прочёл ни разу — проба мёртвая")
    case.check(not seen["bad"], f"атомарность: читатель видел половину: {seen['bad']}")
    # Запись поверх того же файла (open «w») держит inode; temp + replace даёт новый каждый раз.
    case.check(replaced == ATOMIC_WRITES, f"атомарность: новый inode лишь в {replaced} из {ATOMIC_WRITES} записей")
    check_mode(case, stand, "атомарность")
    check_no_litter(case, stand, "атомарность")


def case_timing(case: Case, stand: Stand) -> None:
    now = int(time.time())
    body = json.dumps(session({"five_hour": window(42, now + 3600), "seven_day": window(18, now + 86400)}))
    bare, own = [], []
    for _ in range(TIMING_RUNS):
        started = time.monotonic()
        subprocess.run([sys.executable, "-c", "pass"], capture_output=True, timeout=TIMEOUT)
        bare.append((time.monotonic() - started) * 1000)
        run = stand.run(body)
        if not case.check(run["code"] == 0, f"скорость: код {run['code']}"):
            return
        own.append(run["ms"])
    base, median = statistics.median(bare), statistics.median(own)
    p95 = sorted(own)[int(len(own) * 0.95) - 1]
    load = Path("/proc/loadavg").read_text().split()[0]
    print(f"  скорость: голый запуск {base:.0f} мс, скрипт медиана {median:.0f} мс, p95 {p95:.0f} мс, "
          f"худший {max(own):.0f} мс, load {load}")
    case.check(median - base < OWN_MEDIAN_MAX_MS,
               f"скорость: медиана {median:.0f} мс при голом {base:.0f} мс, своих ≥ {OWN_MEDIAN_MAX_MS}")
    case.check(p95 < P95_FACTOR * (base + OWN_MEDIAN_MAX_MS),
               f"скорость: p95 {p95:.0f} мс ≥ {P95_FACTOR}×({base:.0f}+{OWN_MEDIAN_MAX_MS})")
    case.check(max(own) < SINGLE_MAX_MS, f"скорость: худший вызов {max(own):.0f} мс ≥ {SINGLE_MAX_MS}")


def case_int_and_float(case: Case, stand: Stand) -> None:
    now = int(time.time())
    for label, five, seven in (("целые", 42, 18), ("дробные", 41.6, 0.25), ("края", 0, 100)):
        w5, w7 = window(five, now + 3600), window(seven, now + 86400)
        line = ui_safe(case, stand.run(session({"five_hour": w5, "seven_day": w7})), label)
        data = read_file(case, stand, label)
        check_window(case, data, "five_hour", w5, label)
        check_window(case, data, "seven_day", w7, label)
        case.check(mentions(line, five) and mentions(line, seven), f"{label}: в строке нет {five} и {seven}: {line!r}")


def adapter_read(stand: Stand, now: float) -> dict:
    data = {"лимит_подписки": {"адаптер": "claude", "файл_claude": str(stand.file), "устарел_мин": 15}}
    done = subprocess.run([sys.executable, "-c", DRIVER, str(ADAPTER), json.dumps(data, ensure_ascii=False),
                           str(now)], capture_output=True, text=True, env=stand.env(), timeout=30,
                          cwd=stand.root)
    if done.returncode != 0:
        raise ProbeError(f"адаптер упал: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout)


def case_adapter_reads(case: Case, stand: Stand) -> None:
    now = int(time.time())
    five, seven = window(37.5, now + 3600), window(12, now + 3 * 86400)
    ui_safe(case, stand.run(session({"five_hour": five, "seven_day": seven})), "адаптер, оба окна")
    got = adapter_read(stand, time.time())
    for name, cell in (("пять_часов", five), ("неделя", seven)):
        found = got.get(name) or {}
        case.check(found.get("точность") == "точно" and found.get("процент") == cell["used_percentage"]
                   and found.get("сброс") == cell["resets_at"],
                   f"адаптер: окно {name} прочитано как {found!r}, ждём точно {cell!r}")
    ui_safe(case, stand.run(session({"five_hour": five})), "адаптер, только 5ч")
    found = adapter_read(stand, time.time()).get("пять_часов") or {}
    case.check(found.get("точность") == "точно" and found.get("процент") == five["used_percentage"],
               f"адаптер, только 5ч: окно пять_часов {found!r}")


CASES = {
    "оба_окна": case_both_windows,
    "только_5ч": case_five_hour_only,
    "нет_rate_limits": case_no_rate_limits,
    "окно_пропало": case_window_gone_after_reset,
    "битый_вход": case_broken_input,
    "каталог_без_записи": case_dir_unwritable,
    "атомарность": case_atomic,
    "скорость": case_timing,
    "целое_и_дробное": case_int_and_float,
    "адаптер_читает": case_adapter_reads,
}


def live_file() -> Path | None:
    try:
        path = yaml.safe_load(SPEED_DATA.read_text(encoding="utf-8"))["лимит_подписки"]["файл_claude"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return None
    return Path(path) if path else None


def live_mark(path: Path | None):
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return "нет"
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def run_case(name: str) -> Case:
    case = Case(name)
    with tempfile.TemporaryDirectory(prefix="statusline-limits-") as tmp:
        try:
            CASES[name](case, Stand(Path(tmp)))
        except ProbeError as error:
            case.check(False, str(error))
        except subprocess.TimeoutExpired as error:
            case.check(False, f"вызов дольше {error.timeout} с")
        finally:
            # Случай мог оставить каталог без записи — уборка временного каталога не должна упасть.
            for sub in [Path(tmp), *Path(tmp).rglob("*")]:
                if sub.is_dir() and not sub.is_symlink():
                    os.chmod(sub, 0o700)
    return case


def main(argv: list[str]) -> int:
    only = argv[argv.index("--случай") + 1] if "--случай" in argv else None
    if only and only not in CASES:
        print(f"нет случая {only}; есть: {', '.join(CASES)}")
        return 1
    live = live_file()
    mark = live_mark(live)
    cases = [run_case(name) for name in CASES if not only or name == only]
    guard = Case("живой_файл")
    guard.check(live is not None, f"не найден ключ лимит_подписки.файл_claude в {SPEED_DATA}")
    guard.check(live_mark(live) == mark, f"живой файл лимитов {live} изменён за прогон — стенд протёк")
    for case in [*cases, guard]:
        print(f"{'ок' if not case.failures else 'КРАСНЫЙ'} {case.name} ({case.checks} проверок)")
        for reason in case.failures:
            print(f"  - {reason}")
    checks = sum(c.checks for c in [*cases, guard])
    failures = sum(len(c.failures) for c in [*cases, guard])
    print(f"СТРОКА-ЛИМИТЫ: проб {len(cases)}, проверок {checks}, провалов {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
