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
Прогон: python3 scripts/hooks/test_launch_plan_guard.py
"""
from __future__ import annotations

import importlib.util
import json
import os
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
    old = time.time() - (30 + 5) * 60
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

    slow = tmp / "медленный-launch-plan.py"
    slow.write_text("import time\ntime.sleep(3)\n", encoding="utf-8")
    for limit, expected in ((1, "таймаут"), (10, None)):
        s = setup(tmp / f"таймаут-{limit}", "красный", plan_runs=False)
        s.set_roles({"строгость": "красный", "таймаут_плана_с": limit})
        started = time.monotonic()
        code, err, ctx = hook(s, agent("Часть: P/c/W\nВеди.", subagent_type=LEAD),
                              launch_plan=slow)
        spent = time.monotonic() - started
        row = last(s)
        check(f"таймаут_плана_с {limit} из данных: код 0, skipped, сбой {expected}",
              (0, "skipped", expected),
              (code, row.get("outcome"), (row.get("data") or {}).get("сбой")))
        if limit == 1:
            check("таймаут 1 с: хук не ждал 3 с медленного вызова", True, spent < 3)


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


CASES = (case_red, case_yellow, case_door, case_skip, case_self_derive, case_stale,
         case_trifle_silent, case_stale_cache, case_order, case_failure, case_tasks_plan)


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
