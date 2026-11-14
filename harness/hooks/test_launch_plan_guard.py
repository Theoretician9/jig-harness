#!/usr/bin/env python3
"""Пробы хука плана запуска (scripts/hooks/launch_plan_guard.py) — до кода.

Спека: docs/specs/2026-10-29-правила-вложенности-кодом.md, §3.5–§3.7, строки
§9 «хук 1–5», «жёлтый», «дверь». Решение по задаче хук берёт у
scripts/launch-plan.py (контракт — docstring scripts/test_launch_plan.py).

══════════════════════ КОНТРАКТ launch_plan_guard.py ═══════════════════════

PreToolUse на Agent/Task. Вход — JSON на stdin, как у agent_budget_guard.py:
  {tool_name, session_id, agent_id?, agent_type?,
   tool_input {prompt, description, subagent_type, model}}.
Чужой инструмент — код 0, строки журнала нет.

Задача — из метки в prompt: «Часть: <путь>[#<n>]», регулярка
  Часть:\\s*(?P<path>[^\\s#]+)(?:#(?P<n>\\d+))?
задача — последний сегмент пути через «/»; без #n — вся задача (запуск
управляющего). Метки нет — пропуск: код 0, строка skipped, почему «без метки»
(wip/HARNESS_TASK задачей не считаются: спека и ревью получили бы чужой отказ).

Решение — $LOG_DIR/планы-запуска/<задача>.json; файла нет ИЛИ он старше
вывод_роли.предохранитель.окно_живых минут (mtime файла по часам HARNESS_NOW
или времени) — хук зовёт HARNESS_LAUNCH_PLAN (по умолчанию
scripts/launch-plan.py) `--json <задача>` с тем же окружением и читает файл:
решение «0, ждать» при прежней ёмкости не живёт дольше окна. Предел вызова —
вывод_роли.таймаут_плана_с секунд (данные, не константа). Строгость —
вывод_роли.строгость из HARNESS_HELPER_ROLES.
Состояние решения «мелкая» — хук молчит: код 0, без текста модели, строка
skipped (почему «мелкая»), нарушения не судятся (спека §1, §3.6.1).

Отказы (§3.5), проверяются по порядку спеки 1, 2, 3, 4, 5; data.отказ — первый
сработавший, data.нарушения — список всех сработавших:
  1  роль lead, а исполнителю (subagent_type ≠ harness-lead) в prompt даны
     абсолютные пути файлов (realpath) из ≥ 2 групп частей;
  2  subagent_type harness-lead, а роль по правилам harness-worker;
  3  метка с #n, а части n в плане запуска нет; либо путь главы в метке
     (сегменты до задачи) не равен «глава» решения;
  4  исход_плана нужен_план, а плана нет (план: null);
  5  запусков исполнителей задачи (строки хука outcome ok, subagent_type ≠
     harness-lead, за окно_живых) уже не меньше «исполнителей».
красный: код 2, stderr — «роль по правилам: <роль> (<правило>, групп G,
  файлов F)» и дверь «--door»; строка nesting.decision outcome refused.
жёлтый: код 0, тот же текст в stdout
  {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": …}};
  строка outcome ok, data.предупреждение true.
Пропуск без нарушений — код 0, строка ok, data.отказ null.
План устарел (хеш_плана ≠ sha256 файла-источника) — предупреждение со словом
«устарел» в additionalContext, код 0 (сам по себе не отказ).
Битые данные — код 0, строка skipped с data.почему. Своя ошибка хука (любое
исключение; tool_input не словарь — тоже) — код 0, строка skipped с
data.сбой "сбой"; вызов launch-plan.py дольше таймаут_плана_с — skipped с
data.сбой "таймаут" (метрики §7 считают сбои по data.сбой).
Дверь: `launch_plan_guard.py --door 'причина'` (≥ 10 знаков, иначе код 1) —
$LOG_DIR/launch-plan.door {почему, до} на 15 минут по часам HARNESS_NOW;
пока открыта, отказ становится проходом: код 0, строка ok, data.дверь = причина.
Журнал: $LOG_DIR/вложенность.jsonl, src scripts/hooks/launch_plan_guard.py,
event nesting.decision, data {ид, часть, роль, правило, отказ, предупреждение,
дверь, строгость, почему}.

═════════════════════════════════════════════════════════════════════════════

Стенд — тот же, что у test_launch_plan.py (Stand оттуда): свои журналы, карта,
планы, копии данных. Механизм — рядом либо HARNESS_LAUNCH_PLAN_GUARD (дверь
для мутаций); launch-plan.py — HARNESS_LAUNCH_PLAN.
Мелкая и при ёмкости 0 (слово владельца 30.10, Н-10): план запуска мелочи —
исполнителей 1, ждать false; хук молчит, отказа 5 нет.

Цена хука (замер 30.10 15:20–15:24, стенд этой пробы, 4 ядра, нагрузка 30–34,
лог /var/log/harness/nesting-r2-hooktime.log), по 10 прогонов:
  таймаут 1 с: стена мин 4,08 / медиана 4,56 / макс 5,54 с; CPU (user+sys хука
    и детей) 0,21 / 0,23 / 0,25 с;
  решение из кэша: стена 0,93 / 2,98 / 4,51 с; CPU 0,17 / 0,18 / 0,21 с;
  `python3 -c pass`: стена 0,37 / 0,74 с, CPU 0,04 с; импорт хука (yaml,
    events…): стена 1,28 / 2,43 с, CPU 0,16 с.
Стену делает очередь к процессору (≈7 задач на ядро), не хук: без нагрузки
хук в случае таймаута — CPU 0,25 + ожидание 1 с ≤ 1,5 с. Поэтому проба судит
три вещи: стена < 5 с (грубая, под нагрузкой > 30 может не уложиться — макс
5,54), метка «медленный вызов не досидел» и CPU-время ≤ 1,5 с — две последние
от нагрузки не зависят.

Прогон: python3 scripts/hooks/test_launch_plan_guard.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GUARD = Path(os.environ.get("HARNESS_LAUNCH_PLAN_GUARD") or HERE / "launch_plan_guard.py")
_spec = importlib.util.spec_from_file_location("launch_plan_probe",
                                               HERE.parent / "test_launch_plan.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)
LAUNCH_PLAN = probe.LAUNCH_PLAN
HOOK_SRC = probe.HOOK_SRC
LEAD, WORKER = probe.LEAD, probe.WORKER
DOOR_REASON = "проверка двери пробой: lead на простую задачу"
# Окно стенда — из своей копии данных, не боевое значение helper-roles.yaml.
WINDOW_MIN = probe.ROLES["вывод_роли"]["предохранитель"]["окно_живых"]
HOOK_CPU_MAX_S = 1.5
SLOW_WALL_MAX_S = 5

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def env(stand, now: float | None = None) -> dict:
    return {**stand.env(now), "HARNESS_LAUNCH_PLAN": str(LAUNCH_PLAN)}


def hook(stand, payload, now: float | None = None, raw: str | None = None,
         launch_plan: Path | None = None) -> tuple:
    """(код, stderr, additionalContext)."""
    run_env = env(stand, now)
    if launch_plan is not None:
        run_env["HARNESS_LAUNCH_PLAN"] = str(launch_plan)
    proc = subprocess.run([sys.executable, str(GUARD)], env=run_env,
                          input=raw if raw is not None else json.dumps(payload, ensure_ascii=False),
                          capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        out = {}
    context = str(((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or "")
    return proc.returncode, proc.stderr, context


def door(stand, reason: str) -> int:
    return subprocess.run([sys.executable, str(GUARD), "--door", reason], env=env(stand),
                          capture_output=True, text=True, timeout=60).returncode


def agent(prompt: str, subagent_type: str = WORKER, tool: str = "Agent") -> dict:
    return {"tool_name": tool, "session_id": "проба",
            "tool_input": {"prompt": prompt, "description": "исполнитель",
                           "subagent_type": subagent_type, "model": "sonnet"}}


def hook_rows(stand) -> list:
    return [r for r in stand.rows() if r.get("src") == HOOK_SRC]


def last(stand) -> dict:
    rows = hook_rows(stand)
    return rows[-1] if rows else {}


def setup(base: Path, strict: str, plan_runs: bool = True):
    """L — параллель (lead, 3 группы), W — простая (1 исполнитель, 2 группы),
    N — нужен_план, плана нет."""
    s = probe.Stand(base, roles={"строгость": strict})
    s.task_in_plan("L", probe.plain_parts(s, "l", 3))
    s.task_in_plan("W", probe.plain_parts(s, "w", 2, price="простая"))
    s.card("N", level="крупная")
    s.pretask("N", files=[s.file("src/n1.py")])
    s.write()
    if plan_runs:
        for task in ("L", "W", "N"):
            s.run(task)
    return s


def files_of(stand, task: str) -> list:
    return [p["файлы"][0] for p in stand.plans["P"]["части"][task]]


def refusal_inputs(stand) -> list:
    l1, l2, _ = files_of(stand, "L")
    return [
        (1, agent(f"Часть: P/c/L#1\nПравь {l1} и {l2}.")),
        (2, agent("Часть: P/c/W\nВеди задачу.", subagent_type=LEAD)),
        (3, agent("Часть: P/c/L#9\nПравь.")),
        (4, agent("Часть: N#1\nПравь.")),
    ]


def case_red(tmp: Path) -> None:
    print("красный: пять отказов, проход по правилам")
    s = setup(tmp / "красный", "красный")
    l1 = files_of(s, "L")[0]
    code, err, ctx = hook(s, agent(f"Часть: P/c/L#1\nПравь {l1}."))
    check("красный: исполнитель на одну группу задачи lead — проход", 0, code)
    check("красный: проход — строка ok, отказ null", ("ok", None),
          (last(s).get("outcome"), (last(s).get("data") or {}).get("отказ")))
    check("красный: свежий план — без «устарел» (Н-19)", False, "устарел" in ctx)
    for number, payload in refusal_inputs(s):
        code, err, ctx = hook(s, payload)
        check(f"красный, отказ {number}: код 2", 2, code)
        check(f"красный, отказ {number}: «роль по правилам:» и --door", True,
              "роль по правилам:" in err and "--door" in err)
        row = last(s)
        check(f"красный, отказ {number}: строка refused с номером", ("refused", number),
              (row.get("outcome"), (row.get("data") or {}).get("отказ")))
    code, err, ctx = hook(s, agent("Часть: P/c/W\nВеди.", subagent_type=LEAD))
    check("красный, отказ 2: текст называет роль, правило, группы и файлы", True,
          f"роль по правилам: {WORKER} (простая, групп 2, файлов 2)" in err)

    w1 = files_of(s, "W")[0]
    code, err, ctx = hook(s, agent(f"Часть: P/c/W#1\nПравь {w1}."))
    check("красный: первый исполнитель простой задачи — проход", 0, code)
    code, err, ctx = hook(s, agent("Часть: P/c/W#2\nПравь."))
    check("красный, отказ 5: второй исполнитель на простую — код 2", 2, code)
    check("красный, отказ 5: номер в строке", 5, (last(s).get("data") or {}).get("отказ"))


def case_yellow(tmp: Path) -> None:
    print("жёлтый: те же входы — предупреждение и событие")
    s = setup(tmp / "жёлтый", "жёлтый")
    inputs = refusal_inputs(s)
    w1 = files_of(s, "W")[0]
    for number, payload in inputs:
        code, err, ctx = hook(s, payload)
        row = last(s)
        check(f"жёлтый, отказ {number}: код 0", 0, code)
        check(f"жёлтый, отказ {number}: предупреждение «роль по правилам:»", True,
              "роль по правилам:" in ctx)
        check(f"жёлтый, отказ {number}: строка ok, отказ {number}, предупреждение",
              ("ok", number, True),
              (row.get("outcome"), (row.get("data") or {}).get("отказ"),
               (row.get("data") or {}).get("предупреждение")))
    hook(s, agent(f"Часть: P/c/W#1\nПравь {w1}."))
    code, err, ctx = hook(s, agent("Часть: P/c/W#2\nПравь."))
    check("жёлтый, отказ 5: код 0 и предупреждение", (0, True), (code, "роль по правилам:" in ctx))
    check("жёлтый, отказ 5: номер в строке", 5, (last(s).get("data") or {}).get("отказ"))


def case_door(tmp: Path) -> None:
    print("дверь: 15 минут, потом снова отказ")
    s = setup(tmp / "дверь", "красный")
    lead_on_simple = agent("Часть: P/c/W\nВеди.", subagent_type=LEAD)
    check("дверь: короткая причина не открывает — код 1", 1, door(s, "коротко"))
    check("дверь: без двери — код 2", 2, hook(s, lead_on_simple)[0])
    check("дверь: открыта — код 0", 0, door(s, DOOR_REASON))
    check("дверь: тот же вход при открытой двери — код 0", 0, hook(s, lead_on_simple)[0])
    check("дверь: след — строка ok с причиной двери", ("ok", DOOR_REASON),
          (last(s).get("outcome"), (last(s).get("data") or {}).get("дверь")))
    check("дверь: через 16 мин (подмена часов) — снова код 2", 2,
          hook(s, lead_on_simple, now=time.time() + 16 * 60)[0])


def case_skip(tmp: Path) -> None:
    print("пропуски: без метки, чужой инструмент, битые данные")
    s = setup(tmp / "пропуск", "красный")
    before = len(hook_rows(s))
    code, err, ctx = hook(s, agent("Ревью спеки, без метки.", subagent_type=LEAD))
    check("без метки: код 0 даже при красном", 0, code)
    check("без метки: строка skipped «без метки»", ("skipped", "без метки"),
          (last(s).get("outcome"), (last(s).get("data") or {}).get("почему")))
    rows_now = len(hook_rows(s))
    check("без метки: ровно одна новая строка", before + 1, rows_now)
    code, err, ctx = hook(s, agent("Часть: P/c/W\n", subagent_type=LEAD, tool="Bash"))
    check("чужой инструмент: код 0", 0, code)
    check("чужой инструмент: строки нет", rows_now, len(hook_rows(s)))
    check("битый вход stdin: код 0", 0, hook(s, None, raw="{не json")[0])

    s.set_roles({}, raw="вывод_роли: [не закрыт\n")
    code, err, ctx = hook(s, agent("Часть: P/c/W\nВеди.", subagent_type=LEAD))
    check("битый helper-roles.yaml: код 0", 0, code)
    check("битый helper-roles.yaml: строка skipped с причиной", ("skipped", True),
          (last(s).get("outcome"), bool((last(s).get("data") or {}).get("почему"))))

    s = setup(tmp / "нет-карты", "красный", plan_runs=False)
    s.devmap.unlink()
    code, err, ctx = hook(s, agent("Часть: P/c/W\nВеди.", subagent_type=LEAD))
    check("нет карты и плана запуска: код 0", 0, code)
    check("нет карты: строка skipped с причиной", ("skipped", True),
          (last(s).get("outcome"), bool((last(s).get("data") or {}).get("почему"))))


def case_self_derive(tmp: Path) -> None:
    print("плана запуска нет — хук выводит сам")
    s = setup(tmp / "сам", "красный", plan_runs=False)
    code, err, ctx = hook(s, agent("Часть: P/c/W\nВеди.", subagent_type=LEAD))
    check("сам: lead на простую без плана запуска — код 2", 2, code)
    check("сам: план запуска W записан", True, (s.logs / "планы-запуска" / "W.json").exists())


def case_stale(tmp: Path) -> None:
    print("план устарел — предупреждение")
    s = setup(tmp / "устарел", "красный")
    plan_file = s.plans_dir / "P.md"
    text = plan_file.read_text(encoding="utf-8")
    plan_file.write_text(text.replace(probe.PLAN_COMMENT,
                                      probe.PLAN_COMMENT + "\n# правка после плана запуска"),
                         encoding="utf-8")
    l1 = files_of(s, "L")[0]
    code, err, ctx = hook(s, agent(f"Часть: P/c/L#1\nПравь {l1}."))
    check("устарел: код 0", 0, code)
    check("устарел: предупреждение «устарел»", True, "устарел" in ctx)


def case_trifle_silent(tmp: Path) -> None:
    print("мелкая — хук молчит (Н-2)")
    s = probe.Stand(tmp / "мелкая", roles={"строгость": "красный"})
    s.card("M")
    s.pretask("M", files=[s.file("src/m.py")])
    s.write()
    s.run("M")
    code, err, ctx = hook(s, agent("Часть: M#1\nПравь.", subagent_type=LEAD))
    check("мелкая: lead на мелочь в красном — код 0 (контроль молчит)", 0, code)
    check("мелкая: ни текста модели, ни stderr", ("", ""), (ctx, err))
    check("мелкая: строка skipped", "skipped", last(s).get("outcome"))


def case_trifle_capacity_zero(tmp: Path) -> None:
    print("мелкая при ёмкости 0 — хук молчит, отказа 5 нет (Н-10, слово 30.10)")
    s = probe.Stand(tmp / "мелкая-ёмкость-0", roles={"строгость": "красный"})
    s.card("M")
    s.pretask("M", files=[s.file("src/m.py")])
    probe.fill_capacity(s)
    s.write()
    s.run("M")
    saved = json.loads((s.logs / "планы-запуска" / "M.json").read_text(encoding="utf-8"))
    check("мелкая при ёмкости 0: план запуска — свободно 0, исполнителей 1, ждать false",
          (0, 1, False), ((saved.get("ёмкость") or {}).get("свободно"),
                          saved.get("исполнителей"), saved.get("ждать")))
    for n in (1, 2):
        code, err, ctx = hook(s, agent(f"Часть: M#{n}\nПравь."))
        check(f"мелкая при ёмкости 0, запуск {n}: код 0, без текста, skipped, отказа нет",
              (0, "", "", "skipped", None),
              (code, err, ctx, last(s).get("outcome"), (last(s).get("data") or {}).get("отказ")))


def case_stale_cache(tmp: Path) -> None:
    print("решение старше окна пересчитывается (Н-2)")
    s = setup(tmp / "кэш", "красный")
    saved = s.logs / "планы-запуска" / "W.json"
    frozen = json.loads(saved.read_text(encoding="utf-8"))
    frozen.update(исполнителей=0, ждать=True)
    w1 = files_of(s, "W")[0]
    payload = agent(f"Часть: P/c/W#1\nПравь {w1}.")

    saved.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
    code, err, ctx = hook(s, payload)
    check("кэш: свежее решение «0, ждать» хук берёт как есть — отказ 5 (контроль)",
          (2, 5), (code, (last(s).get("data") or {}).get("отказ")))

    saved.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
    old = time.time() - (WINDOW_MIN + 5) * 60
    os.utime(saved, (old, old))
    code, err, ctx = hook(s, payload)
    check("кэш: решение старше окна_живых — пересчёт, первый исполнитель проходит",
          (0, None), (code, (last(s).get("data") or {}).get("отказ")))
    renewed = json.loads(saved.read_text(encoding="utf-8"))
    check("кэш: файл решения переписан (исполнителей 1)", 1, renewed.get("исполнителей"))


def case_order(tmp: Path) -> None:
    print("порядок отказов 1–5 и глава метки (Н-7)")
    s = setup(tmp / "порядок", "красный")
    s.card("E", level="крупная")
    s.write()
    s.run("E")
    code, err, ctx = hook(s, agent("Часть: E#2\nПравь."))
    data = last(s).get("data") or {}
    check("порядок: частей нет и плана нет — отказ 3 (раньше 4), код 2", (2, 3),
          (code, data.get("отказ")))
    check("порядок: в строке все сработавшие нарушения [3, 4]", [3, 4], data.get("нарушения"))
    l1 = files_of(s, "L")[0]
    code, err, ctx = hook(s, agent(f"Часть: P/чужая/L#1\nПравь {l1}."))
    check("глава метки P/чужая ≠ глава карточки P/c — отказ 3", (2, 3),
          (code, (last(s).get("data") or {}).get("отказ")))


def case_failure(tmp: Path) -> None:
    print("сбой и таймаут хука видны журналу (Н-9)")
    s = setup(tmp / "сбой", "красный")
    code, err, ctx = hook(s, None, raw=json.dumps(
        {"tool_name": "Agent", "session_id": "проба", "tool_input": "не словарь"}))
    row = last(s)
    check("сбой: своя ошибка — код 0, строка skipped, data.сбой «сбой»",
          (0, "skipped", "сбой"), (code, row.get("outcome"), (row.get("data") or {}).get("сбой")))

    for limit, expected in ((1, "таймаут"), (10, None)):
        # Метку медленный вызов пишет, только досидев свои 3 с: есть метка — хук ждал.
        marker = tmp / f"досидел-{limit}"
        slow = tmp / f"медленный-launch-plan-{limit}.py"
        slow.write_text(f"import pathlib, time\ntime.sleep(3)\n"
                        f"pathlib.Path({str(marker)!r}).write_text('1')\n", encoding="utf-8")
        s = setup(tmp / f"таймаут-{limit}", "красный", plan_runs=False)
        s.set_roles({"строгость": "красный", "таймаут_плана_с": limit})
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.monotonic()
        code, err, ctx = hook(s, agent("Часть: P/c/W\nВеди.", subagent_type=LEAD),
                              launch_plan=slow)
        spent = time.monotonic() - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        row = last(s)
        check(f"таймаут_плана_с {limit} из данных: код 0, skipped, сбой {expected}",
              (0, "skipped", expected),
              (code, row.get("outcome"), (row.get("data") or {}).get("сбой")))
        check(f"таймаут_плана_с {limit}: медленный вызов {'убит до конца' if limit == 1 else 'досидел (контроль метки)'}",
              limit != 1, marker.exists())
        if limit == 1:
            # Стена под нагрузкой машины плывёт (замер в docstring), порог стены — с
            # запасом; метка и CPU-время хука от нагрузки не зависят.
            cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
            check(f"таймаут 1 с: хук не ждал 3 с медленного вызова (стена < {SLOW_WALL_MAX_S} с)",
                  True, spent < SLOW_WALL_MAX_S)
            check(f"таймаут 1 с: CPU-время хука (user+sys детей) ≤ {HOOK_CPU_MAX_S} с, "
                  f"вышло {cpu:.2f} с", True, cpu <= HOOK_CPU_MAX_S)


def case_tasks_plan(tmp: Path) -> None:
    print("план в .tasks/<id>/план.md — нет отказа 4")
    s = probe.Stand(tmp / "план-в-tasks", roles={"строгость": "красный"})
    s.card("N", level="крупная")
    s.pretask("N", files=[s.file("src/n1.py")])
    (s.root / ".tasks" / "N" / "план.md").write_text("# План: N\n", encoding="utf-8")
    s.write()
    s.run("N")
    code, err, ctx = hook(s, agent("Часть: N#1\nПравь."))
    check("план в .tasks: исполнитель — код 0, отказа нет", (0, None),
          (code, (last(s).get("data") or {}).get("отказ")))


# ─────────── HELPER_WORKTREE: исполнитель — в своём дереве (Т2) ───────────
# Спека задачи §5, Р-4: isolation_verdict() — первой проверкой
# decide, ДО пропуска «без метки»; ключ — установленный harness.conf (HARNESS_CONF), затем
# ROOT/harness/config/harness.conf; дверь --door этот отказ не открывает.
ISOLATION_HINT = 'isolation: "worktree"'


def with_key(stand, value: str | None) -> None:
    text = probe.CONF_TEXT + (f'HELPER_WORKTREE="{value}"\n' if value is not None else "")
    stand.conf.write_text(text, encoding="utf-8")


def launch(prompt: str, subagent_type: str = WORKER, isolation: str | None = None) -> dict:
    payload = agent(prompt, subagent_type)
    if isolation is not None:
        payload["tool_input"]["isolation"] = isolation
    return payload


def case_isolation_key_off(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: нет — красна только при требовании isolation и при ключе 0.
    print("HELPER_WORKTREE=0: isolation не смотрится")
    s = setup(tmp / "изоляция-0", "красный")
    l1 = files_of(s, "L")[0]
    for value in (None, "0", "да"):
        with_key(s, value)
        before = len(hook_rows(s))
        code, err, ctx = hook(s, launch(f"Часть: P/c/L#1\nПравь {l1}."))
        check(f"ключ {value!r}: исполнитель без isolation — проход, код 0", 0, code)
        check(f"ключ {value!r}: ни слова об isolation", False,
              "isolation" in err or "isolation" in ctx)
        check(f"ключ {value!r}: одна строка журнала, как раньше", 1, len(hook_rows(s)) - before)
    with_key(s, "0")
    code, err, _ = hook(s, launch("Без метки: ревью.", subagent_type=WORKER))
    check("ключ 0: без метки — пропуск, как раньше", 0, code)


def case_isolation_key_on(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: проверка после пропуска «без метки» (ловит строка
    # «без метки → 2»); отказ всем типам (ловят explorer и lead без isolation); отказ
    # вместо старой логики при isolation (ловит «L#9 → отказ 3»).
    print("HELPER_WORKTREE=1: исполнитель без isolation — отказ")
    s = setup(tmp / "изоляция-1", "красный")
    with_key(s, "1")
    l1 = files_of(s, "L")[0]
    code, err, _ = hook(s, launch(f"Часть: P/c/L#1\nПравь {l1}."))
    check("исполнитель без isolation — код 2", 2, code)
    check("отказ называет поле isolation: \"worktree\"", True, ISOLATION_HINT in err)
    check("отказ называет очередь слияния", True, "merge-queue.py" in err)
    # Поломка: «есть поле isolation» вместо «isolation == worktree» — чужое значение прошло бы.
    code, err, _ = hook(s, launch(f"Часть: P/c/L#1\nПравь {l1}.", isolation="remote"))
    check("исполнитель с isolation не «worktree» — код 2", 2, code)
    code, err, _ = hook(s, launch(f"Часть: P/c/L#1\nПравь {l1}.", isolation="worktree"))
    check("исполнитель с isolation — старая логика: проход", 0, code)
    code, err, _ = hook(s, launch("Часть: P/c/L#9\nПравь.", isolation="worktree"))
    check("исполнитель с isolation — старая логика: отказ 3 по плану", (2, False),
          (code, ISOLATION_HINT in err))
    code, err, _ = hook(s, launch("Без метки: правь файл.", subagent_type=WORKER))
    check("без метки «Часть:» — всё равно код 2 (проверка до пропуска)", 2, code)
    code, err, _ = hook(s, launch("Без метки: найди.", subagent_type="harness-explorer"))
    check("harness-explorer без isolation — проход", 0, code)
    code, err, _ = hook(s, launch("Часть: P/c/L\nВеди задачу.", subagent_type=LEAD))
    check("управляющий без isolation — проход", 0, code)
    code, err, _ = hook(s, launch("Часть: P/c/L\nВеди задачу.", subagent_type=LEAD,
                                  isolation="worktree"))
    check("управляющий С isolation — код 2", 2, code)
    check("отказ управляющему называет основное дерево", True, "основном дереве" in err)
    check("дверь открыта", 0, door(s, DOOR_REASON))
    code, _, _ = hook(s, launch(f"Часть: P/c/L#1\nПравь {l1}."))
    check("дверь плана запуска отказ isolation не открывает", 2, code)


def case_isolation_installed_wins(tmp: Path) -> None:
    # Поломка, что оставила бы зелёной: ключ только из дерева или только из установленного —
    # одна из двух строк краснеет.
    print("HELPER_WORKTREE: установленный конфиг старше конфига дерева")
    s = setup(tmp / "изоляция-конфиг", "красный")
    tree = tmp / "дерево-хука"
    (tree / "scripts" / "hooks").mkdir(parents=True)
    (tree / "harness" / "config").mkdir(parents=True)
    (tree / "scripts" / "lib").symlink_to(HERE.parent / "lib")
    copy = tree / "scripts" / "hooks" / "launch_plan_guard.py"
    copy.write_bytes(GUARD.read_bytes())
    (tree / "harness" / "config" / "harness.conf").write_text('HELPER_WORKTREE="1"\n',
                                                               encoding="utf-8")
    l1 = files_of(s, "L")[0]
    payload = json.dumps(launch(f"Часть: P/c/L#1\nПравь {l1}."), ensure_ascii=False)

    def run_copy() -> int:
        return subprocess.run([sys.executable, str(copy)], env=env(s), input=payload,
                              capture_output=True, text=True, timeout=120).returncode

    with_key(s, "0")
    check("установленный 0, дерево 1 — проход", 0, run_copy())
    with_key(s, None)
    check("в установленном ключа нет, дерево 1 — отказ", 2, run_copy())


CASES = (case_red, case_yellow, case_door, case_skip, case_self_derive, case_stale,
         case_trifle_silent, case_trifle_capacity_zero, case_stale_cache, case_order,
         case_failure, case_tasks_plan, case_isolation_key_off, case_isolation_key_on,
         case_isolation_installed_wins)


def main() -> int:
    global checked, failed
    missing = [str(p) for p in (GUARD, LAUNCH_PLAN) if not p.exists()]
    if missing:
        names = ", ".join(Path(m).name for m in missing)
        print(f"нет {names}: {'; '.join(missing)}")
        print("SELFTEST: КРАСНЫЙ (механизма нет, случаи не запускались)")
        return 1
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for case in CASES:
            try:
                case(tmp)
            except Exception as exc:  # noqa: BLE001 — падение случая = провал, не падение пробы
                checked += 1
                failed += 1
                print(f"  ПЛОХО {case.__name__}: исключение {type(exc).__name__}: {exc}")
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} проверок, "
          f"{len(CASES)} случаев, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
