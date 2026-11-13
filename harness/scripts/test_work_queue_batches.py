#!/usr/bin/env python3
"""Пробы пачек однотипных задач прибора очереди (`work-queue.py --пачки`).

Владелец 12.11.2026: «бери пачки задач… потом прогоняю все тесты… не теряя
качество» и «системно сделать чтобы в обновлениях других тоже это работало».
Спека: .tasks/hr.vot-eto-vot-bolee-effektivnuyu/спека.md.

Контракт:
  а. карточки одного выведенного ключа — одна пачка, ключ печатается целиком;
  б. другой ключ, тот же путь при другом уровне, карточки без ключа (сколько бы
     их ни было) и группа из одной карточки — одиночки;
  в. done/принято/беклог, test без провала, заблокированная — ни в пачке, ни в
     одиночках; test с провалом — в пачке;
  г. потолок берётся из pipeline.yaml (`пачка.задач`): 8 при 6 → 6 + 2 подряд
     в порядке порядок(); 7 → 6 + одиночка; другой потолок в файле — другая нарезка;
  д. явное поле `пачка:` перебивает выведенный ключ;
  е. CLI `--пачки`: «пачек: N из M задач», строки «<ключ>: id, …», «одиночки: …».

Стенд — во временном каталоге: копия work-queue.py с копией scripts/lib, своя
harness/config/pipeline.yaml (только блок пачки) и копия боевого queue.yaml
рядом, своя карта, свой каталог логов. Исходник — из `WORK_QUEUE_SCRIPT` (дверь
для копии под мутацией), по умолчанию соседний `work-queue.py`.

Прогон:
    python3 scripts/test_work_queue_batches.py
Последняя строка: «ОЧЕРЕДЬ-ПАЧКИ: путей N, неудач M»; код 0 — все зелёные.
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
TMP = Path(tempfile.mkdtemp(prefix="test-work-queue-batches-"))
LOGS = TMP / "log"
LOGS.mkdir()
(TMP / "scripts").mkdir()
shutil.copytree(HERE / "lib", TMP / "scripts" / "lib",
                ignore=shutil.ignore_patterns("__pycache__"))
SCRIPT = TMP / "scripts" / "work-queue.py"
shutil.copy(SOURCE, SCRIPT)
CONF_DIR = TMP / "harness" / "config"
CONF_DIR.mkdir(parents=True)
if LIVE_QUEUE_CONF.is_file():
    shutil.copy(LIVE_QUEUE_CONF, CONF_DIR / "queue.yaml")
PIPELINE = CONF_DIR / "pipeline.yaml"
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
    print(f"ОЧЕРЕДЬ-ПАЧКИ: путей {paths}, неудач {failures}")
    sys.exit(1 if failures else 0)


def set_cap(tasks: int) -> None:
    PIPELINE.write_text(f"пачка: {{задач: {tasks}, помощников: 3}}\n", encoding="utf-8")


set_cap(6)
try:
    spec = importlib.util.spec_from_file_location("work_queue_batches", SCRIPT)
    wq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wq)
    for name in ("ключ_пачки", "настройки_пачки", "пачки"):
        if not callable(getattr(wq, name, None)):
            raise AttributeError(f"нет функции {name}()")
except Exception as exc:  # noqa: BLE001 — красный прогон обязан назвать причину
    check(False, "", f"work-queue.py без API пачек: {type(exc).__name__}: {exc}")
    finish()


def card(ид: str, status: str = "plan", характер: str | None = "починка",
         уровень: str | None = "обычная", **поля) -> dict:
    карточка = {"id": ид, "status": status, "title": ид, "summary": "с"}
    if характер or уровень:
        карточка["глубина"] = {"уровень": уровень, "характер": характер}
    карточка.update(поля)
    return карточка


def alert(ид: str, **поля) -> dict:
    return card(ид, источник=f"тревога:session-warden:{ид}", **поля)


def by_cmd(ид: str, путь: str, **поля) -> dict:
    return card(ид, проверка={"команда": f"python3 {путь} --selftest", "ждём": "зелено"}, **поля)


def stand(*tasks: dict) -> dict:
    return {"queue": [], "lines": [{"id": "h", "tasks": list(tasks)}]}


def batches_of(data: dict, cap: int | None = None) -> tuple[dict, list]:
    got, singles = wq.пачки(data, направление="h", потолок=cap)
    return {tuple(п["задачи"]): п["ключ"] for п in got}, list(singles)


def in_batches(got: dict) -> set:
    return {ид for ids in got for ид in ids}


# ── ключ ───────────────────────────────────────────────────────────────────
print("── ключ ──")
check(wq.ключ_пачки(alert("к1")) == "починка/обычная/тревога:session-warden",
      "тревога: ключ «характер/уровень/тревога:<сторож>»",
      f"ключ тревоги {wq.ключ_пачки(alert('к1'))!r}")
check(wq.ключ_пачки(by_cmd("к2", "scripts/x.py", характер=None, уровень=None)) == "-/-/scripts/x.py",
      "без глубины: «-/-/<путь из проверка.команда>»",
      f"ключ без глубины {wq.ключ_пачки(by_cmd('к2', 'scripts/x.py', характер=None, уровень=None))!r}")
check(wq.ключ_пачки(card("к3", summary="правка scripts/x.py")) is None,
      "путь в summary ключом не считается — только проверка.команда",
      f"ключ из summary {wq.ключ_пачки(card('к3', summary='правка scripts/x.py'))!r}")
check(wq.ключ_пачки(by_cmd("к4", "scripts/x.py", пачка="общая")) == "общая",
      "явное `пачка:` — ключ целиком",
      f"ключ явного поля {wq.ключ_пачки(by_cmd('к4', 'scripts/x.py', пачка='общая'))!r}")

# ── а, б: одна пачка из трёх, остальное одиночки ───────────────────────────
print("── а, б ──")
A = [alert("а1"), alert("а2"), alert("а3")]
OTHER = by_cmd("б.другой", "scripts/other.py")
OTHER_LEVEL = alert("б.уровень", уровень="мелкая")
NO_KEY = card("б.безключа")
got, singles = batches_of(stand(*A, OTHER, OTHER_LEVEL, NO_KEY))
check(got == {("а1", "а2", "а3"): "починка/обычная/тревога:session-warden"},
      "а: три карточки одного ключа — ровно одна пачка из трёх",
      f"а: пачки {got}")
check(sorted(singles) == ["б.безключа", "б.другой", "б.уровень"],
      "б: другой ключ, другой уровень, без ключа, группа из одной — одиночки",
      f"б: одиночки {singles}")
got, singles = batches_of(stand(NO_KEY, card("б.безключа2"), card("б.безключа3")))
check(not got and sorted(singles) == ["б.безключа", "б.безключа2", "б.безключа3"],
      "б: несколько карточек без ключа — одиночки, а не пачка",
      f"б: без ключа — пачки {got}, одиночки {singles}")

# ── в: закрытые не идут никуда, test с провалом — в пачке ───────────────────
print("── в ──")
CLOSED = [alert("в.done", status="done"), alert("в.принято", status="принято"),
          alert("в.беклог", status="беклог"), alert("в.test", status="test"),
          alert("в.блок", blocked="ждём владельца")]
FAILED = alert("в.провал", status="test", провал_проверки="2026-11-12: не сошлось")
got, singles = batches_of(stand(*A, *CLOSED, FAILED))
closed_ids = {к["id"] for к in CLOSED}
check(not (in_batches(got) | set(singles)) & closed_ids,
      "в: done/принято/беклог/test без провала/заблокированная — ни в пачке, ни в одиночках",
      f"в: закрытые всплыли: {sorted((in_batches(got) | set(singles)) & closed_ids)}")
check({frozenset(k) for k in got} == {frozenset({"а1", "а2", "а3", "в.провал"})},
      "в: test с провалом — открытая работа, идёт в пачку",
      f"в: пачки {got}")

# ── г: потолок из конфига ──────────────────────────────────────────────────
print("── г ──")
EIGHT = [by_cmd(f"г{н}", "scripts/eight.py") for н in range(1, 9)]
order = [м["id"] for м in wq.порядок(stand(*EIGHT), направление="h", внимание={},
                                     тревоги={}, без_потолка=True)]
check(wq.настройки_пачки(PIPELINE).get("задач") == 6,
      "стенд: настройки_пачки() читает «задач: 6» из pipeline.yaml",
      f"стенд: настройки_пачки() = {wq.настройки_пачки(PIPELINE)}")
got, singles = batches_of(stand(*EIGHT))
check(list(got) == [tuple(order[:6]), tuple(order[6:])] and not singles,
      "г: 8 при потолке 6 из файла — пачки 6 и 2 подряд в порядке порядок()",
      f"г: пачки {list(got)}, одиночки {singles}, порядок {order}")
got, singles = batches_of(stand(*EIGHT[:7]))
check([len(k) for k in got] == [6] and len(singles) == 1,
      "г: 7 при потолке 6 — пачка 6 и одиночка (пачка из одной — не пачка)",
      f"г: семь — пачки {list(got)}, одиночки {singles}")
set_cap(3)
got, _ = batches_of(stand(*EIGHT))
check([len(k) for k in got] == [3, 3, 2],
      "г: потолок 3 в файле — нарезка 3, 3, 2 (число не зашито в код)",
      f"г: при потолке 3 из файла {[len(k) for k in got]}")
got, _ = batches_of(stand(*EIGHT), cap=4)
check([len(k) for k in got] == [4, 4],
      "г: явный потолок вызова 4 перебивает файл",
      f"г: при потолке 4 {[len(k) for k in got]}")
PIPELINE.write_text("помощники: {}\n", encoding="utf-8")
check(wq.настройки_пачки(PIPELINE) == {"задач": 6, "помощников": 3},
      "г: нет блока `пачка:` — умолчания {задач: 6, помощников: 3}",
      f"г: без блока {wq.настройки_пачки(PIPELINE)}")
set_cap(6)

# ── д: явное поле перебивает выведенный ключ ───────────────────────────────
print("── д ──")
EXPLICIT = [by_cmd("д1", "scripts/one.py", пачка="общая"),
            by_cmd("д2", "scripts/two.py", пачка="общая")]
STOLEN = alert("д.своя", пачка="другая")
got, singles = batches_of(stand(*A, *EXPLICIT, STOLEN))
check(got.get(("д1", "д2")) == "общая",
      "д: разные выведенные ключи, одно `пачка:` — одна пачка «общая»",
      f"д: пачки {got}")
check("д.своя" not in in_batches(got) and "д.своя" in singles
      and got.get(("а1", "а2", "а3")) == "починка/обычная/тревога:session-warden",
      "д: карточка с `пачка:` уходит из группы своего выведенного ключа",
      f"д: пачки {got}, одиночки {singles}")

# ── е: CLI ─────────────────────────────────────────────────────────────────
print("── е ──")
MAP.write_text(yaml.safe_dump(stand(*A, *CLOSED, OTHER, NO_KEY, *EXPLICIT),
                              allow_unicode=True, sort_keys=False), encoding="utf-8")
done = subprocess.run([sys.executable, str(SCRIPT), str(MAP), "--пачки"],
                      capture_output=True, text=True, timeout=60, env=ENV)
out = done.stdout.splitlines()
check(done.returncode == 0 and out[:1] == ["пачек: 2 из 7 задач"],
      "е: первая строка «пачек: 2 из 7 задач», код 0",
      f"е: код {done.returncode}, вывод {out[:3]}, ошибки {done.stderr.strip()[-200:]!r}")
check("починка/обычная/тревога:session-warden: а1, а2, а3" in out and "общая: д1, д2" in out,
      "е: строка на пачку «<ключ>: id, id, …»",
      f"е: строки пачек {out[1:]}")
singles_line = next((l for l in out if l.startswith("одиночки: ")), "")
check(sorted(singles_line.removeprefix("одиночки: ").split(", ")) == ["б.безключа", "б.другой"],
      "е: строка «одиночки: …» — ровно открытые одиночки",
      f"е: одиночки {singles_line!r}")

finish()
