#!/usr/bin/env python3
"""Пробы полос тревог прибора очереди (`work-queue.py`): проверки — ночью, аварии — днём.

Владелец 05.11.2026: тревоги ПРОВЕРОК (мутации, ночные ворота, ревизии) не
должны попадать в дневную очередь и в пинки сторожа — их берёт ночной демон.
Карточка-тревога узнаётся по `источник: тревога:<ключ>`, полоса — по префиксу
ключа из `полосы_тревог.проверки` в harness/config/queue.yaml.

Контракт (hr.checks-alerts-separate-lane):
  1. днём `--порядок`, `--готовые`, `--следующая` и «работа: N» не называют
     тревогу проверки, даже стоящую в `queue`; живая авария и тревога с
     НЕИЗВЕСТНЫМ префиксом остаются днём (неизвестное — авария);
  2. `--проверки` печатает «проверок: N» и ровно «id ключ» открытых тревог
     проверок по всей карте — без аварий и обычных задач;
  3. проверка в статусе test (без провала) или done не выдаётся и ночью;
  4. нет раздела `полосы_тревог` (или он пуст) — не фильтруется ничего;
  5. `порядок()` без `настройки` всё равно фильтрует: полосы читаются из файла.

Стенд — во временном каталоге: копия work-queue.py с копией scripts/lib и своим
harness/config/queue.yaml рядом (прибор ищет настройки возле себя), своя карта,
свой каталог логов. Боевая карта и боевой queue.yaml не меняются. Исходник
прибора — из `WORK_QUEUE_SCRIPT` (дверь для копии под мутацией), по умолчанию
соседний `work-queue.py`; данные полос — копия боевого queue.yaml, чтобы проба
держала и сам список префиксов.

Прогон:
    python3 scripts/test_work_queue_lanes.py
Последняя строка: «ОЧЕРЕДЬ-ПОЛОСЫ: путей N, неудач M»; код 0 — все зелёные.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("WORK_QUEUE_SCRIPT") or HERE / "work-queue.py")
LIVE_QUEUE_CONF = HERE.parent / "harness" / "config" / "queue.yaml"
TMP = Path(tempfile.mkdtemp(prefix="test-work-queue-lanes-"))
LOGS = TMP / "log"
LOGS.mkdir()
(TMP / "scripts").mkdir()
# Копия, а не ссылка: мутация копии прибора не должна дотянуться до боевого lib.
shutil.copytree(HERE / "lib", TMP / "scripts" / "lib",
                ignore=shutil.ignore_patterns("__pycache__"))
SCRIPT = TMP / "scripts" / "work-queue.py"
shutil.copy(SOURCE, SCRIPT)
QUEUE_CONF = TMP / "harness" / "config" / "queue.yaml"
QUEUE_CONF.parent.mkdir(parents=True)
LIVE_CONF_TEXT = LIVE_QUEUE_CONF.read_text(encoding="utf-8")
QUEUE_CONF.write_text(LIVE_CONF_TEXT, encoding="utf-8")
INSTALL_CONF = TMP / "install.conf"
INSTALL_CONF.write_text(f'LOG_DIR="{LOGS}"\n', encoding="utf-8")
HARNESS_CONF = TMP / "harness.conf"
HARNESS_CONF.write_text('ACTIVE_LINE="h"\n', encoding="utf-8")
os.environ.update({"HARNESS_INSTALL_CONF": str(INSTALL_CONF), "HARNESS_CONF": str(HARNESS_CONF),
                   "LOG_DIR": str(LOGS), "HARNESS_ACTIVE_LINE": "h"})
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
MAP = TMP / "dev-map.yaml"

paths = failures = 0


def check(condition: bool, good: str, bad: str) -> None:
    global paths, failures
    paths += 1
    if condition:
        print("ок  · " + good)
    else:
        failures += 1
        print("ПЛОХО · " + bad)


def finish() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"ОЧЕРЕДЬ-ПОЛОСЫ: путей {paths}, неудач {failures}")
    sys.exit(1 if failures else 0)


try:
    spec = importlib.util.spec_from_file_location("work_queue_lanes", SCRIPT)
    wq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wq)
except Exception as exc:  # noqa: BLE001 — красный прогон обязан назвать причину
    check(False, "", f"work-queue.py не загружен: {type(exc).__name__}: {exc}")
    finish()

# Посылка стенда: прибор читает именно копию queue.yaml, и в ней есть префикс
# мутаций. Без неё зелёный ниже значил бы «настроек не нашёл», а не «отфильтровал».
prefixes = (wq.настройки().get("полосы_тревог") or {}).get("проверки") or []
check("mutations-changed:" in prefixes and "heartbeat-watch:" not in prefixes,
      f"стенд: полосы прочитаны из копии queue.yaml ({len(prefixes)} префиксов проверок)",
      f"стенд: префиксы проверок {prefixes!r} — нет mutations-changed: или авария в списке")

CHECK = {"id": "т.проверка", "status": "plan", "title": "мутации", "summary": "с",
         "источник": "тревога:mutations-changed:red"}
CHECK_FAILED = {"id": "т.проверка-провал", "status": "test", "title": "ворота", "summary": "с",
                "провал_проверки": "2026-11-05: не сошлось",
                "источник": "тревога:nightly-gates:red"}
CHECK_DONE_TEST = {"id": "т.проверка-test", "status": "test", "title": "р", "summary": "с",
                   "источник": "тревога:memory-revision:x"}
CHECK_DONE = {"id": "т.проверка-done", "status": "done", "title": "р", "summary": "с",
              "источник": "тревога:project-revision:x"}
CHECK_OTHER_LINE = {"id": "т.проверка-чужая", "status": "plan", "title": "м", "summary": "с",
                    "источник": "тревога:task-mutations:hr.x"}
CRASH = {"id": "т.авария", "status": "plan", "title": "сторож", "summary": "с",
         "источник": "тревога:heartbeat-watch:dead:x"}
UNKNOWN = {"id": "т.новая", "status": "plan", "title": "новое", "summary": "с",
           "источник": "тревога:нечто-новое:y"}
PLAIN = {"id": "т.обычная", "status": "plan", "title": "работа", "summary": "с"}

# Проверка стоит и в `queue`: явная очередь не должна протащить её днём.
STAND = {"queue": [CHECK["id"]], "lines": [
    {"id": "h", "tasks": [CHECK, CHECK_FAILED, CHECK_DONE_TEST, CHECK_DONE, CRASH, UNKNOWN, PLAIN]},
    {"id": "другое", "tasks": [CHECK_OTHER_LINE]}]}
DAY_IDS = {CRASH["id"], UNKNOWN["id"], PLAIN["id"]}
CHECK_IDS = {CHECK["id"], CHECK_FAILED["id"], CHECK_DONE_TEST["id"], CHECK_DONE["id"],
             CHECK_OTHER_LINE["id"]}
NIGHT_EXPECTED = [f"{CHECK['id']} mutations-changed:red",
                  f"{CHECK_FAILED['id']} nightly-gates:red",
                  f"{CHECK_OTHER_LINE['id']} task-mutations:hr.x"]


def run(data: dict, *flags: str) -> tuple[int, list]:
    MAP.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    done = subprocess.run([sys.executable, str(SCRIPT), str(MAP), *flags],
                          capture_output=True, text=True, timeout=60, env=ENV)
    return done.returncode, done.stdout.splitlines()


def mentioned(lines: list, ids: set) -> set:
    """Какие id из `ids` названы в выводе — словом целиком, не подстрокой."""
    words = {w.strip("·—:;=,()") for l in lines for w in l.split()}
    words |= {w.split("=")[0] for w in words}
    return ids & words


def line_with(lines: list, prefix: str) -> str:
    return next((l for l in lines if l.startswith(prefix)), "")


# ── 1. днём проверок нет, аварии и неизвестное — есть ─────────────────────
print("── днём ──")
_, out = run(STAND, "--порядок")
check(not mentioned(out, CHECK_IDS),
      "--порядок: ни одной тревоги проверки (и в «ждут», и в «queue»)",
      f"--порядок называет проверки {sorted(mentioned(out, CHECK_IDS))}: {out}")
check(mentioned(out, DAY_IDS) == DAY_IDS,
      "--порядок: авария, неизвестный префикс и обычная задача на месте",
      f"--порядок потерял {sorted(DAY_IDS - mentioned(out, DAY_IDS))}: {out}")
work_line = line_with(out, "работа: ")
check(work_line == f"работа: {len(DAY_IDS)}",
      f"«работа: {len(DAY_IDS)}» — проверки в счёт не идут",
      f"строка работы {work_line!r}, ждали «работа: {len(DAY_IDS)}»")
now_line = line_with(out, "сейчас делаем: ")
check(now_line.split(" — ")[0].removeprefix("сейчас делаем: ") in DAY_IDS,
      "«сейчас делаем» — дневная задача",
      f"«сейчас делаем»: {now_line!r}")

_, out = run(STAND, "--готовые")
ready = out[1:]
check(out[:1] == [f"готовых: {len(DAY_IDS)}"] and set(ready) == DAY_IDS,
      f"--готовые: «готовых: {len(DAY_IDS)}» — авария, неизвестное, обычная",
      f"--готовые: {out}, ждали ровно {sorted(DAY_IDS)}")

# Проверка стоит в queue и потому без фильтра обгоняла бы аварию: стенд из двух
# карточек, чтобы голову решал именно фильтр, а не прочие веса.
rc, out = run({"queue": [CHECK["id"]], "lines": [{"id": "h", "tasks": [CHECK, CRASH]}]},
              "--следующая")
first = out[0] if out else ""
check(rc == 0 and first.startswith(f"СЛЕДУЮЩАЯ РАБОТА: {CRASH['id']} —"),
      "--следующая: авария, а не проверка из queue",
      f"--следующая: код {rc}, {first!r}")
rc, out = run({"queue": [], "lines": [{"id": "h", "tasks": [CHECK]}]}, "--следующая")
first = out[0] if out else ""
check(rc == 1 and first.startswith("СЛЕДУЮЩЕЙ РАБОТЫ НЕТ"),
      "--следующая: одна проверка в карте — днём работы нет, код 1",
      f"--следующая, одна проверка: код {rc}, {first!r}")
_, out = run({"queue": [], "lines": [{"id": "h", "tasks": [CHECK]}]}, "--порядок")
check(line_with(out, "работа: ") == "работа: 0",
      "одна проверка в карте — «работа: 0» (сторож не будит)",
      f"одна проверка в карте: {line_with(out, 'работа: ')!r}")

# Строка «ждут:» строится своим разбором карты, не порядком: заблокированная
# проверка не должна всплыть днём и через неё, а заблокированная авария — должна.
BLOCKED_CHECK = dict(CHECK, id="т.проверка-блок", blocked="ждём владельца")
BLOCKED_CRASH = dict(CRASH, id="т.авария-блок", blocked="ждём сервер")
_, out = run({"queue": [], "lines": [{"id": "h", "tasks": [BLOCKED_CHECK, BLOCKED_CRASH]}]},
             "--порядок")
waiting = line_with(out, "ждут: ")
check(BLOCKED_CRASH["id"] in waiting and BLOCKED_CHECK["id"] not in waiting,
      "«ждут:» называет заблокированную аварию, но не заблокированную проверку",
      f"«ждут:» {waiting!r}")

# ── 2. ночью — ровно проверки ──────────────────────────────────────────────
print("── ночью ──")
_, out = run(STAND, "--проверки")
check(out[:1] == [f"проверок: {len(NIGHT_EXPECTED)}"],
      f"--проверки: «проверок: {len(NIGHT_EXPECTED)}»",
      f"--проверки: первая строка {out[:1]}, ждали «проверок: {len(NIGHT_EXPECTED)}»")
check(sorted(out[1:]) == sorted(NIGHT_EXPECTED),
      "--проверки: ровно «id ключ» открытых проверок, и из чужого направления",
      f"--проверки: {out[1:]}, ждали {NIGHT_EXPECTED}")

# ── 3. сделанная проверка не выдаётся и ночью ─────────────────────────────
night_ids = {l.split()[0] for l in out[1:] if l.split()}
check(not night_ids & {CHECK_DONE_TEST["id"], CHECK_DONE["id"]},
      "--проверки: test без провала и done не выдаются",
      f"--проверки выдала сделанные: {sorted(night_ids & {CHECK_DONE_TEST['id'], CHECK_DONE['id']})}")

# ── 4. нет раздела полос — ничего не пропадает ────────────────────────────
print("── без раздела полос ──")
OPEN_CHECKS = {CHECK["id"], CHECK_FAILED["id"]}
for label, text in (("раздела нет", "потолок_строк: 10\n"),
                    ("раздел пуст", "полосы_тревог:\n"),
                    ("список пуст", "полосы_тревог:\n  проверки: []\n")):
    QUEUE_CONF.write_text(text, encoding="utf-8")
    _, out = run(STAND, "--готовые")
    got = set(out[1:])
    check(got == DAY_IDS | OPEN_CHECKS,
          f"{label}: --готовые отдаёт и проверки, и дневные — фильтра нет",
          f"{label}: --готовые {sorted(got)}, ждали {sorted(DAY_IDS | OPEN_CHECKS)}")
    _, out = run(STAND, "--проверки")
    check(out == ["проверок: 0"],
          f"{label}: --проверки — «проверок: 0»",
          f"{label}: --проверки {out}")
QUEUE_CONF.write_text(LIVE_CONF_TEXT, encoding="utf-8")

# ── 5. порядок() без настроек читает файл ─────────────────────────────────
print("── порядок() без настроек ──")
ids = {м["id"] for м in wq.порядок(STAND, направление="h", внимание={}, тревоги={},
                                   без_потолка=True)}
check(not ids & CHECK_IDS and DAY_IDS <= ids,
      "порядок() без настроек: проверок нет, дневные на месте",
      f"порядок() без настроек: {sorted(ids)}")
ids = {м["id"] for м in wq.порядок(STAND, направление="", внимание={}, тревоги={},
                                   без_потолка=True, проверки=True)}
check(ids == {CHECK["id"], CHECK_FAILED["id"], CHECK_OTHER_LINE["id"]},
      "порядок(проверки=True) без настроек: ровно открытые проверки",
      f"порядок(проверки=True) без настроек: {sorted(ids)}")
check(wq.разобрать(STAND, направление="h")[0] == len(DAY_IDS),
      f"разобрать() без настроек: работа {len(DAY_IDS)}",
      f"разобрать() без настроек: {wq.разобрать(STAND, направление='h')}")

finish()
