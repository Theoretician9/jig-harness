#!/usr/bin/env python3
"""Пробы «сейчас делаем» прибора очереди (`work-queue.py`) против статусов.

Голову порядка читают панель (`harness/panel/state.py`, «сейчас делаем:» у
`--порядок`), осмотр сессии, сторож сессий и сторож остановки (`--следующая`,
«бери её, если стоишь»), /status владельца (`--владельцу`). Сделанная работа во
главе зовёт смену переделывать готовое, а начатая теряется.

Контракт (hr.queue-current-ignores-status):
  1. есть `wip` — «сейчас делаем» называет её; при нескольких — прежний
     порядок среди wip;
  2. `test` с полем `провал_проверки` — ОТКРЫТАЯ работа: приёмка нашла
     поломку, починка за сменой. Ярус открытых (ниже wip, выше сделанных
     test), может быть головой, входит в «работа: N»;
  3. `test` без провала — сделана и ждёт проверки: головой НИКОГДА
     (`следующая()`, `--следующая`, `--порядок`, «Дальше» для владельца),
     даже со словом владельца и со свободным пунктом; `принято`, `done`,
     `беклог` — не работа вовсе;
  4. «работа: N» считается по тем же местам, что голова: иначе сторож будит
     «работа: 1» при «СЛЕДУЮЩЕЙ РАБОТЫ НЕТ»;
  5. без wip — прежний порядок, но только среди открытых.

Стенд — своя карта и свой журнал внимания во временном каталоге; живая карта
не читается. Модуль берётся по пути из `WORK_QUEUE_SCRIPT` (дверь для копии
под мутацией), по умолчанию — соседний `work-queue.py`.

Прогон:
    python3 scripts/test_work_queue.py
Последняя строка: «ОЧЕРЕДЬ-СТАТУСЫ: путей N, неудач M»; код 0 — все зелёные.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SCRIPT = Path(os.environ.get("WORK_QUEUE_SCRIPT") or HERE / "work-queue.py")
TMP = Path(tempfile.mkdtemp(prefix="test-work-queue-"))
LOGS = TMP / "log"
LOGS.mkdir()
INSTALL_CONF = TMP / "install.conf"
INSTALL_CONF.write_text(f'LOG_DIR="{LOGS}"\n', encoding="utf-8")
HARNESS_CONF = TMP / "harness.conf"
HARNESS_CONF.write_text('ACTIVE_LINE="h"\n', encoding="utf-8")
os.environ.update({"HARNESS_INSTALL_CONF": str(INSTALL_CONF), "HARNESS_CONF": str(HARNESS_CONF),
                   "LOG_DIR": str(LOGS), "HARNESS_ACTIVE_LINE": "h"})

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
    print(f"ОЧЕРЕДЬ-СТАТУСЫ: путей {paths}, неудач {failures}")
    sys.exit(1 if failures else 0)


try:
    spec = importlib.util.spec_from_file_location("work_queue", SCRIPT)
    wq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wq)
except Exception as exc:  # noqa: BLE001 — красный прогон обязан назвать причину
    check(False, "", f"work-queue.py не загружен: {type(exc).__name__}: {exc}")
    finish()

SETTINGS = wq.НАСТРОЙКИ_ПО_УМОЛЧАНИЮ
WORD_ID = "1792290300.056874669"           # форма источника живой карточки
WORD = {WORD_ID: "в первую очередь"}
SOURCE = f"inbox:{WORD_ID}"
NOT_WORK = ("принято", "done", "беклог")   # не работа ни с каким полем
FAILED = "2026-10-18: команда вернула 1; вывод: /var/log/harness/priyomka/t.вывод"
FREE_ITEM = {"checklist": [{"label": "а", "done": True}, {"label": "б"}]}
# Форма живой карточки test с провалом приёмки: без чеклиста, проверяет
# механизм (owner_testable: false), источник — текст, а не inbox.
LIVE_FAILED = {
    "id": "hr.dver-udaleniya-ne-pomnit-vopros", "status": "test",
    "провал_проверки": "2026-10-18: команда вернула 1; вывод: "
                       "/var/log/harness/priyomka/hr.dver-udaleniya-ne-pomnit-vopros.вывод",
    "owner_testable": False,
    "источник": "живой случай: голый глагол не принят дверью удаления",
    "глубина": {"уровень": "обычная", "характер": "разработка", "кем": "смена",
                "когда": "2026-10-17"},
    "title": "Дверь удаления не помнит, о чём я спрашивал",
    "проверка": {"команда": "python3 scripts/protected-delete.py --selftest",
                 "ждём": "SELFTEST: зелёный"},
    "when": {"started": "2026-10-17", "done": "2026-10-17", "tested": None, "estimated": False},
}


def stand(*tasks, queue=()) -> dict:
    return {"queue": list(queue), "lines": [{"id": "h", "tasks": list(tasks)}]}


def head(data: dict, attention: dict | None = None):
    """Что прибор назовёт «сейчас делаем»."""
    found = wq.следующая(data, направление="h", настройки=SETTINGS, внимание=attention or {})
    return found["id"] if found else None


def order_ids(data: dict, attention: dict | None = None) -> list:
    return [м["id"] for м in wq.порядок(data, направление="h", настройки=SETTINGS,
                                        внимание=attention or {})]


def work_count(data: dict) -> int:
    return wq.разобрать(data, направление="h")[0]


# ── 1. есть wip — она и называется ────────────────────────────────────────
print("── есть wip ──")
# Сделанная test без чеклиста и без «чем_подтверждено», с его словом
# «в первую очередь», рядом — настоящая wip: слово весит больше начатого.
live = stand({"id": "сделана", "status": "test", "источник": SOURCE},
             {"id": "идёт", "status": "wip"})
got = head(live, WORD)
check(got == "идёт", "test с его словом не перебивает wip",
      f"«сейчас делаем» = {got!r}, ждали «идёт» (wip)")
places = order_ids(live, WORD)
check((places[0] if places else None) == got, "следующая() и порядок()[0] называют одно (первой стоит wip)",
      f"следующая() = {got!r}, порядок()[0] = {places[:1]} — два ответа одного прибора")

# Провал весит больше wip: ярус открытых обязан стоять НИЖЕ начатого.
failed = stand({"id": "провал", "status": "test", "провал_проверки": FAILED},
               {"id": "идёт", "status": "wip"})
got = head(failed)
check(got == "идёт", "test с провалом проверки не перебивает wip",
      f"«сейчас делаем» = {got!r}, ждали «идёт»: test с провалом встал над wip")

# Открытая plan его словом тоже не перебивает начатое: судит СТАТУС, а не вес.
word_plan = stand({"id": "просил", "status": "plan", "источник": SOURCE},
                  {"id": "идёт", "status": "wip"})
got = head(word_plan, WORD)
check(got == "идёт", "plan с его словом не перебивает wip",
      f"«сейчас делаем» = {got!r}, ждали «идёт»: вес слова встал над статусом wip")

# Без слова и без провала: очередь 20 + почти закончена 10 + свежесть 9 = 39 > 30.
# Поломка «убрать только слово и провал» эту пробу оставит красной.
heavy_plan = stand({"id": "тяжёлая", "status": "plan", "источник": SOURCE, **FREE_ITEM},
                   {"id": "идёт", "status": "wip"}, queue=["тяжёлая"])
got = head(heavy_plan)
check(got == "идёт", "plan в очереди, почти закрытая и свежая не перебивает wip",
      f"«сейчас делаем» = {got!r}, ждали «идёт»: веса plan сложились выше wip")

# Несколько wip — прежний порядок СРЕДИ wip, а не порядок строк файла:
# вторая в файле стоит в очереди и обязана идти первой.
two_wip = stand({"id": "первая-в-файле", "status": "wip"},
                {"id": "вторая-в-очереди", "status": "wip"},
                {"id": "план-со-словом", "status": "plan", "источник": SOURCE},
                queue=["вторая-в-очереди"])
got = head(two_wip, WORD)
check(got == "вторая-в-очереди", "две wip: первой — по прежнему порядку среди wip",
      f"«сейчас делаем» = {got!r}, ждали «вторая-в-очереди»")

# Блокированная wip без свободной работы прячется — берётся свободная wip.
blocked_wip = stand({"id": "ждёт", "status": "wip", "blocked": "ждём его слова"},
                    {"id": "идёт", "status": "wip"},
                    {"id": "сделана", "status": "test", "источник": SOURCE})
got = head(blocked_wip, WORD)
check(got == "идёт", "заблокированная wip пропущена, называется свободная wip, не test",
      f"«сейчас делаем» = {got!r}, ждали «идёт»")

# ── 2. сделанная test и закрытые статусы головой не бывают ────────────────
print("── сделанная test и закрытые ──")
# По каждому статусу ОТДЕЛЬНО: поломка, выкинувшая из множества один статус,
# видна своей строкой. Слово и свободный пункт — оба подъёма разом; провал
# добавлен только закрытым: у test он делает работу открытой (п.2).
for status in ("test",) + NOT_WORK:
    closed = {"id": f"закрыта-{status}", "status": status, "источник": SOURCE, **FREE_ITEM}
    if status != "test":
        closed["провал_проверки"] = FAILED
    data = stand(closed, {"id": "открытая", "status": "plan"})
    got = head(data, WORD)
    check(got == "открытая", f"{status} со словом и свободным пунктом не называется",
          f"{status}: «сейчас делаем» = {got!r}, ждали «открытая»")

# Каждый подъём сделанной test ПО ОТДЕЛЬНОСТИ (без wip).
lifts = {
    "слово без «чем_подтверждено»": {"источник": SOURCE},
    "свободный пункт при блокере": {"blocked": "ждёт его слова", **FREE_ITEM},
    "незакрытый пункт и слово": {"источник": SOURCE, **FREE_ITEM},
    "пункт с блокером и слово": {"источник": SOURCE,
                                 "checklist": [{"label": "а", "done": True},
                                               {"label": "б", "blocked": "его цифра"}]},
}
for name, fields in lifts.items():
    data = stand({"id": "сделана", "status": "test", **fields},
                 {"id": "открытая", "status": "plan"})
    got = head(data, WORD)
    check(got == "открытая", f"test ({name}) без wip не называется — называется plan",
          f"test ({name}): «сейчас делаем» = {got!r}, ждали «открытая»")

# Только закрытые и сделанные — работы нет: None, а не сделанная задача.
only_closed = stand(*({"id": f"з-{s}", "status": s, "источник": SOURCE}
                      for s in ("test",) + NOT_WORK))
got = head(only_closed, WORD)
check(got is None, "на карте только сделанные и закрытые — «сейчас делаем» пусто",
      f"на карте только сделанные и закрытые, а названа {got!r}")

# Сделанная test со свободным пунктом — ЕДИНСТВЕННЫЙ кандидат. Сортировка
# здесь ничего не решает: голова обязана отсечь ярус сделанных сама.
only_done_free = stand({"id": "сделана-с-пунктом", "status": "test", **FREE_ITEM})
got = head(only_done_free)
check(got is None, "единственная test со свободным пунктом головой не становится",
      f"на карте одна сделанная test, а «сейчас делаем» = {got!r}")
count = work_count(only_done_free)
check(count == 0, "одна сделанная test со свободным пунктом — «работа: 0»",
      f"«работа: {count}» при одной сделанной test: сторож будит, а следующей работы нет")

# ── 3. test с провалом — открытая работа ──────────────────────────────────
print("── провал — открытая работа ──")
# Живая форма: без чеклиста, owner_testable: false. Рядом простая plan:
# провал весит 40 > 0, в ярусе открытых он первый.
live_failed = stand(dict(LIVE_FAILED), {"id": "открытая", "status": "plan"})
got = head(live_failed)
check(got == LIVE_FAILED["id"], "test с провалом (живая форма) без wip — «сейчас делаем»",
      f"«сейчас делаем» = {got!r}, ждали {LIVE_FAILED['id']!r}: провал выпал из работы")
count = work_count(stand(dict(LIVE_FAILED)))
check(count == 1, "одна test с провалом — «работа: 1»",
      f"«работа: {count}» при одной test с провалом: долг починки не виден никому")

# Внутри яруса открытых — прежние веса: слово владельца (100) выше провала (40).
# Поломка «провал в ярус wip» здесь красит.
word_over_failed = stand(dict(LIVE_FAILED), {"id": "просил", "status": "plan", "источник": SOURCE})
got = head(word_over_failed, WORD)
check(got == "просил", "открытая plan с его словом выше провала — веса яруса прежние",
      f"«сейчас делаем» = {got!r}, ждали «просил»: провал встал над весами открытых")

# Ярус, а не вес: сделанная test с пунктом, словом и очередью весит 130 > 40,
# и всё равно идёт ПОСЛЕ провала. Поломка «провал в ярус сделанных» здесь красит.
heavy_done = {"id": "сделана-тяжёлая", "status": "test", "источник": SOURCE, **FREE_ITEM}
failed_vs_done = stand(heavy_done, {"id": "провал", "status": "test", "провал_проверки": FAILED},
                       queue=["сделана-тяжёлая"])
got = head(failed_vs_done, WORD)
check(got == "провал", "провал — голова и при более тяжёлой сделанной test",
      f"«сейчас делаем» = {got!r}, ждали «провал»")
places = order_ids(failed_vs_done, WORD)
check("провал" in places and (places.index("провал") < places.index("сделана-тяжёлая")
                              if "сделана-тяжёлая" in places else True),
      "в порядке провал стоит выше сделанной test",
      f"порядок {places}: провал не выше сделанной test")

# ── 4. без wip — прежний порядок среди открытых ───────────────────────────
print("── без wip ──")
word_open = stand({"id": "хвост", "status": "plan"},
                  {"id": "просил", "status": "plan", "источник": SOURCE},
                  {"id": "сделана", "status": "test", "источник": SOURCE})
got = head(word_open, WORD)
check(got == "просил", "без wip его слово поднимает открытую plan",
      f"«сейчас делаем» = {got!r}, ждали «просил»")
fresh = stand({"id": "старая", "status": "plan", "источник": "inbox:1000.0"},
              {"id": "свежая", "status": "plan", "источник": "inbox:9000.0"},
              {"id": "ничья", "status": "plan"})
got = head(fresh)
check(got == "свежая", "без wip хвост plan разводится свежестью источника",
      f"«сейчас делаем» = {got!r}, ждали «свежая»")

# ── 5. ответ владельцу (/status) ──────────────────────────────────────────
# «В плане следующей» и «Дальше» — те же головы порядка; сделанная test туда
# не попадает ни первой, ни второй. Доска и git передаются доводом: проба
# судит прибор, а не машину.
print("── ответ владельцу ──")
DONE_TITLE = "СДЕЛАННАЯ-ЗАДАЧА-ПРОБЫ"
done_free = {"id": "сделана-с-пунктом", "status": "test", "title": DONE_TITLE, **FREE_ITEM}
plan_titled = {"id": "открытая", "status": "plan", "title": "ОТКРЫТАЯ-ЗАДАЧА-ПРОБЫ"}


def owner_lines(data: dict, board: str) -> list:
    return wq.для_владельца(data, сделал=[], доска=board)


lines = owner_lines(stand(done_free, plan_titled), "")
leaked = [l for l in lines if DONE_TITLE in l]
check(any(l.startswith("В плане следующей: ОТКРЫТАЯ") for l in lines) and not leaked,
      "без доски: следующей — открытая, сделанной test нет ни в одной строке",
      f"ответ владельцу {lines}: сделанная test названа работой")
lines = owner_lines(stand(done_free), "")
check(any(l.startswith("Делаю: очередь пуста") for l in lines)
      and not [l for l in lines if DONE_TITLE in l],
      "только сделанная test — «Делаю: очередь пуста»",
      f"ответ владельцу {lines}: при одной сделанной test названа работа")
lines = owner_lines(stand(done_free), "пишу пробы")
check(not [l for l in lines if DONE_TITLE in l], "с доской: «Дальше» не называет сделанную test",
      f"ответ владельцу {lines}: «Дальше» — сделанная test")

# ── вход команды: то, что читают панель и сторожа ─────────────────────────
# Панель берёт «сейчас делаем:» и «работа:» у `--порядок`, сторож остановки —
# «СЛЕДУЮЩАЯ РАБОТА:» у `--следующая`; оба читают журнал внимания из LOG_DIR.
# Поломка, починившая функцию, но не вход (свой журнал, своё направление,
# голова из места[0]), здесь красит.
print("── вход команды ──")
map_path = TMP / "dev-map.yaml"
(LOGS / "внимание.jsonl").write_text(
    json.dumps({"id": WORD_ID, "метка": time.time(), "слово": "в первую очередь"},
               ensure_ascii=False) + "\n", encoding="utf-8")
# Посылка стенда: журнал внимания прочитан. Без неё красный ниже значил бы
# «стенд не тот», а зелёный — «слово не дошло», а не «починено».
heard = wq.внимание_владельца(LOGS / "внимание.jsonl")
check(WORD_ID in heard, "стенд: журнал внимания читается прибором",
      f"стенд: журнал внимания не прочитан: {heard}")
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def run(data: dict, flag: str) -> tuple[int, list]:
    map_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    done = subprocess.run([sys.executable, str(SCRIPT), str(map_path), flag],
                          capture_output=True, text=True, timeout=60, env=env)
    return done.returncode, done.stdout.splitlines()


def line_with(lines: list, prefix: str) -> str:
    return next((l for l in lines if l.startswith(prefix)), "")


_, out = run(live, "--порядок")
now_line = line_with(out, "сейчас делаем: ")
check(now_line.startswith("сейчас делаем: идёт —"), "--порядок (панель): «сейчас делаем: идёт»",
      f"--порядок (панель): {now_line!r}, ждали «сейчас делаем: идёт — …»")
_, out = run(live, "--следующая")
first = out[0] if out else ""
check(first.startswith("СЛЕДУЮЩАЯ РАБОТА: идёт —"),
      "--следующая (сторож остановки): «СЛЕДУЮЩАЯ РАБОТА: идёт»",
      f"--следующая (сторож остановки): {first!r}, ждали «СЛЕДУЮЩАЯ РАБОТА: идёт — …»")

_, out = run(only_done_free, "--порядок")
now_line, work_line = line_with(out, "сейчас делаем: "), line_with(out, "работа: ")
check(now_line == "сейчас делаем: очередь пуста" and work_line == "работа: 0",
      "--порядок, одна сделанная test: «работа: 0» и «сейчас делаем: очередь пуста»",
      f"--порядок, одна сделанная test: {work_line!r} / {now_line!r}")
rc, out = run(only_done_free, "--следующая")
first = out[0] if out else ""
check(rc == 1 and first.startswith("СЛЕДУЮЩЕЙ РАБОТЫ НЕТ"),
      "--следующая, одна сделанная test: «СЛЕДУЮЩЕЙ РАБОТЫ НЕТ», код 1",
      f"--следующая, одна сделанная test: код {rc}, {first!r}")

failed_id = LIVE_FAILED["id"]
_, out = run(live_failed, "--порядок")
now_line = line_with(out, "сейчас делаем: ")
check(now_line.startswith(f"сейчас делаем: {failed_id} —"),
      "--порядок, живая форма провала: он «сейчас делаем»",
      f"--порядок, живая форма провала: {now_line!r}, ждали «сейчас делаем: {failed_id} — …»")
rc, out = run(live_failed, "--следующая")
first = out[0] if out else ""
check(rc == 0 and first.startswith(f"СЛЕДУЮЩАЯ РАБОТА: {failed_id} —"),
      "--следующая, живая форма провала: «СЛЕДУЮЩАЯ РАБОТА» — он",
      f"--следующая, живая форма провала: код {rc}, {first!r}")

finish()
