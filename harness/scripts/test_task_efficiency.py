#!/usr/bin/env python3
"""Пробы отчёта «какие задачи шли неэффективно» (`task-efficiency.py`).

Владелец 07.11: механизм, который по карте задач видит неэффективные задачи
(вариант 1 разбора замысла). Контракт:
  1. по задаче: часов от первого коммита с трейлером `Dev-Map: <id>` до
     перехода в test/done (у открытой — до «сейчас», с пометкой open), число
     коммитов, возвраты статуса (test/done → ниже), строки факта против
     оценки volume.loc; правка самой карты в факт не идёт;
  1а. срок без второй точки во времени (один коммит, он же закрыл задачу) —
     «не замерен» (null), а не ноль: нули тянули медиану характера в 0.0;
  1б. оценка с estimated: true — константа автомата (loc: 100 и подобные),
     её не судят: оценки нет, перерасхода нет, в счёт не идёт;
  2. трейлер с несколькими id через запятую считается каждой задаче;
     id трейлера, которого нет в карте, — в «вне карты», не в задачи;
  3. перенос карточки между линиями — не возврат;
  4. карточки архива карты (`docs/dev-map-archive.yaml`) — тоже задачи; в
     архиве задача не открыта, какой бы статус ни несла;
  5. топ неэффективных: задача с возвратом, долгим сроком и перерасходом —
     первая; `--top N` режет топ; сводка по глубина.характер — медиана
     часов только по задачам с замеренным сроком;
  6. git зовётся не больше двух раз на весь отчёт (история карты — 16 МБ).

Стенд — свой временный git-репозиторий со своей картой и коммитами с
трейлерами; живой проект не читается. Скрипт берётся по пути из
`TASK_EFFICIENCY_SCRIPT` (дверь для копии под мутацией), по умолчанию —
соседний `task-efficiency.py`.

Прогон:
    python3 scripts/test_task_efficiency.py
Последняя строка: «ЭФФЕКТИВНОСТЬ-ЗАДАЧ: путей N, неудач M»; код 0 — все зелёные.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = Path(os.environ.get("TASK_EFFICIENCY_SCRIPT") or HERE / "task-efficiency.py")
TMP = Path(tempfile.mkdtemp(prefix="test-task-efficiency-"))
REPO = TMP / "repo"
REPO.mkdir()
DAY = 86400
T0 = 1_800_000_000                      # день 0 стенда; «сейчас» = день 12

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
    print(f"ЭФФЕКТИВНОСТЬ-ЗАДАЧ: путей {paths}, неудач {failures}")
    sys.exit(1 if failures else 0)


def git(*args: str, when: int | None = None) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{when} +0000"
    subprocess.run(["git", "-C", str(REPO), *args], check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def card(task_id: str, status: str, character: str, loc: int, estimated: str = "true") -> str:
    return (f"      - id: {task_id}\n"
            f"        status: {status}\n"
            f"        глубина: {{ уровень: обычная, характер: {character}, кем: смена }}\n"
            f"        title: \"{task_id}\"\n"
            f"        volume: {{ loc: {loc}, complexity: 2, tokens_k: 10, estimated: {estimated} }}\n")


def write_map(lines: dict) -> None:
    text = "version: 1\nlines:\n"
    for line_id, cards in lines.items():
        text += f"  - id: {line_id}\n    tasks:\n" + "".join(cards)
    (REPO / "dev-map.yaml").write_text(text, encoding="utf-8")


def code(name: str, lines: int) -> None:
    path = REPO / name
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(old + "".join(f"x{i}\n" for i in range(lines)), encoding="utf-8")


def commit(day: float, trailer: str, subject: str = "шаг") -> None:
    git("add", "-A")
    message = f"{subject}\n\nDev-Map: {trailer}\n" if trailer else subject
    git("commit", "-q", "-m", message, when=int(T0 + day * DAY))


def state(slow: str, fast: str, moved_line: str, opened: str = "wip", bounce: str = "plan",
          quick: str = "test") -> dict:
    cards_a = [card("t.slow", slow, "разработка", 10, estimated="false"),
               card("t.quick", quick, "починка", 10),
               card("t.fast", fast, "починка", 50),
               card("t.open", opened, "разработка", 20),
               card("t.nocommit", "plan", "исследование", 30),
               card("t.bounce", bounce, "исследование", 100)]
    moved = card("t.moved", "test", "починка", 5)
    # Первой в линии a и последней в b: перенос — честное удаление и вставка.
    if moved_line == "a":
        return {"a": [moved] + cards_a, "b": [card("t.other", "plan", "починка", 1)]}
    return {"a": cards_a, "b": [card("t.other", "plan", "починка", 1), moved]}


# --- история стенда ---------------------------------------------------------
git("init", "-q")
write_map(state("plan", "plan", "a", opened="plan", quick="plan"))
commit(0, "", "карта")
# t.slow: день 0 начат, 3 — test, 5 — возврат в wip, 10 — снова test.
write_map(state("wip", "plan", "a", opened="plan", quick="plan"))
code("slow.py", 30)
commit(0, "t.slow")
write_map(state("test", "plan", "a", opened="plan", quick="plan"))
code("slow.py", 30)
commit(3, "t.slow")
# t.quick: начата в день 4 и закрыта через три часа — короткая, но замеренная.
write_map(state("test", "plan", "a", opened="plan", quick="wip"))
code("quick.py", 5)
commit(4, "t.quick")
write_map(state("test", "plan", "a", opened="plan", quick="test"))
code("quick.py", 5)
commit(4.125, "t.quick")
write_map(state("wip", "plan", "a", opened="plan"))
code("slow.py", 20)
commit(5, "t.slow")
# Один коммит на две задачи и чужой id вне карты.
write_map(state("wip", "test", "a", opened="plan"))
code("fast.py", 40)
code("slow.py", 20)
commit(6, "t.fast, t.slow")
write_map(state("test", "test", "a", opened="plan"))
code("slow.py", 0)
commit(10, "t.slow")
# Перенос t.moved из линии a в линию b — статус тот же, возврата нет.
write_map(state("test", "test", "b", opened="wip"))
code("open.py", 7)
commit(2, "t.open, t.moved")
code("ghost.py", 3)
commit(11, "t.gone")
# Архив карты: закрытые и убранные карточки — тоже история задач.
(REPO / "docs").mkdir()
(REPO / "docs" / "dev-map-archive.yaml").write_text(
    "version: 1\nlines:\n  - id: a\n    tasks:\n" + card("t.archived", "done", "починка", 10)
    + card("t.shelved", "plan", "разработка", 10), encoding="utf-8")
commit(1, "t.archived")
code("shelved.py", 2)
commit(1, "t.shelved")
# t.bounce: за один день дважды возвращена из test — переделка без срока и строк.
for status in ("test", "wip", "test", "wip", "test"):
    write_map(state("test", "test", "b", opened="wip", bounce=status))
    commit(11, "t.bounce")


def run(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(REPO),
                           "--now", str(T0 + 12 * DAY), *args],
                          capture_output=True, text=True, env=run_env, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


# Счётчик вызовов git: обёртка в PATH пишет строку на каждый вызов.
SHIM = TMP / "bin"
SHIM.mkdir()
CALLS = TMP / "git-calls"
REAL_GIT = shutil.which("git")
(SHIM / "git").write_text(f"#!/bin/sh\necho x >> {CALLS}\nexec {REAL_GIT} \"$@\"\n")
(SHIM / "git").chmod(0o755)
run_env = dict(os.environ, PATH=f"{SHIM}:{os.environ['PATH']}")

rc, out, err = run("--json")
try:
    report = json.loads(out)
except ValueError:
    check(False, "", f"--json не разбирается: код {rc}, stderr {err.strip()[-300:]!r}")
    finish()

tasks = {t["id"]: t for t in report.get("tasks", [])}
check(bool(tasks), "задачи разобраны из стенда", "список задач пуст")
slow = tasks.get("t.slow", {})
fast = tasks.get("t.fast", {})
moved = tasks.get("t.moved")
opened = tasks.get("t.open", {})

check(slow.get("commits") == 5, "t.slow: 5 коммитов (один общий с t.fast)",
      f"t.slow: коммитов {slow.get('commits')}, ждали 5")
check(slow.get("returns") == 1, "t.slow: один возврат test → wip",
      f"t.slow: возвратов {slow.get('returns')}, ждали 1")
check(slow.get("hours") == 240, "t.slow: 240 часов от первого коммита до последнего test",
      f"t.slow: часов {slow.get('hours')}, ждали 240")
check(slow.get("loc_fact") == 140 and slow.get("loc_estimate") == 10 and slow.get("ratio") == 14,
      "t.slow: факт 140 строк против оценки 10 (estimated: false), ×14, карта в факт не идёт",
      f"t.slow: факт {slow.get('loc_fact')}, оценка {slow.get('loc_estimate')}, "
      f"×{slow.get('ratio')}, ждали 140, 10, ×14")
check(fast.get("commits") == 1 and fast.get("returns") == 0 and "hours" in fast
      and fast.get("hours") is None,
      "t.fast: 1 коммит, 0 возвратов, срок не замерен (null, не 0) — трейлер «a, b» считан обеим",
      f"t.fast: {fast!r}")
quick = tasks.get("t.quick", {})
check(quick.get("hours") == 3, "t.quick: 3 часа — короткий срок не округлён в ноль",
      f"t.quick: {quick!r}")
bounce = tasks.get("t.bounce", {})
check("ratio" in bounce and bounce.get("ratio") is None and bounce.get("loc_estimate") is None,
      "оценка estimated: true (константа автомата) не судится: оценки нет, перерасхода нет",
      f"t.bounce: оценка {bounce.get('loc_estimate')!r}, ×{bounce.get('ratio')!r}, ждали null")
check(fast.get("loc_fact") == 60, "t.fast: факт общего коммита 60 строк",
      f"t.fast: факт {fast.get('loc_fact')}, ждали 60")
check(moved is not None and moved.get("returns") == 0 and moved.get("commits") == 1, "перенос карточки между линиями — не возврат",
      f"t.moved: {moved!r}")
check(opened.get("open") is True and opened.get("hours") == 240,
      "t.open: открыта, 240 часов (10 дней) до «сейчас»",
      f"t.open: {opened!r}")
archived = tasks.get("t.archived", {})
shelved = tasks.get("t.shelved", {})
check(archived.get("commits") == 1 and archived.get("open") is False,
      "карточка из архива карты (docs/dev-map-archive.yaml) — в отчёте, закрыта",
      f"t.archived: {archived!r}")
check(shelved.get("open") is False and "hours" in shelved and shelved.get("hours") is None,
      "plan в архиве — не открытая: срок не тянется до «сейчас»",
      f"t.shelved: {shelved!r}")
check("t.nocommit" not in tasks, "задача без коммитов в отчёт не входит",
      "t.nocommit попала в отчёт без коммитов")
check(report.get("outside_map") == 1 and "t.gone" not in tasks,
      "id трейлера вне карты — в «вне карты: 1»",
      f"вне карты {report.get('outside_map')}, t.gone в задачах: {'t.gone' in tasks}")

top = [t["id"] for t in report.get("top", [])]
check(top[:2] == ["t.slow", "t.bounce"],
      "топ неэффективных: t.slow (всё сразу), за ней t.bounce (два возврата за день)",
      f"топ: {top}")
chars = report.get("by_character", {})
dev = chars.get("разработка", {})
check(dev.get("tasks") == 3 and dev.get("returns") == 1 and "починка" in chars,
      "сводка по характеру: разработка — 3 задачи (одна из архива), 1 возврат; починка есть",
      f"сводка: {chars!r}")
fix = chars.get("починка", {})
check(fix.get("median_hours") == 3 and fix.get("timed") == 1 and fix.get("tasks") == 4,
      "починка: медиана 3 ч по одной замеренной из 4 — незамеренные медиану не тянут в ноль",
      f"починка: {fix!r}")
check(chars.get("исследование", {}).get("median_hours", 0) is None,
      "исследование без замеренных сроков: медианы нет (null), а не 0",
      f"исследование: {chars.get('исследование')!r}")

calls = len(CALLS.read_text().splitlines()) if CALLS.exists() else 0
check(0 < calls <= 2, f"git звался {calls} раз(а) на весь отчёт",
      f"git звался {calls} раз, ждали 1–2")

rc, out, err = run("--json", "--top", "1")
try:
    one = json.loads(out).get("top", [])
except ValueError:
    one = None
check(isinstance(one, list) and len(one) == 1, "--top 1 режет топ до одной",
      f"--top 1: {one!r}")

rc, out, err = run("--top", "3")
check(rc == 0 and "t.slow" in out and "возврат" in out and "разработка" in out
      and "оценки нет" in out and "медиана 3.0 ч" in out,
      "текстовый вывод: по-русски, называет t.slow, возвраты, характер, «оценки нет», медиану в часах",
      f"текст: код {rc}, {out[-400:]!r} {err[-200:]!r}")
check(len(out.splitlines()) <= 20, f"текстовый вывод короткий: {len(out.splitlines())} строк",
      f"текстовый вывод длинный: {len(out.splitlines())} строк")

finish()
