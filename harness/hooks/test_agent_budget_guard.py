#!/usr/bin/env python3
"""Проба потолка помощников на задачу (scripts/hooks/agent_budget_guard.py).

Случай: план 2026-10-28 «ускорение кодом», пункт 2 — число помощников на задачу
не судил никто, мелкая задача получала 3–5 ревьюеров по привычке скила.

Что судит, на стенде (свой LOG_DIR, своя карта, свой pipeline.yaml):
  * ВСЕ уровни из `помощники` настоящего pipeline.yaml — список берётся
    ПАРСИНГОМ, пустой список = провал пробы (иначе зелень без работы);
  * потолок N: N запусков проходят, N+1-й — отказ с числом и дверью;
    0 — без потолка: 20 запусков подряд проходят;
  * потолок ревьюеров отдельно от общего;
  * числа берутся из ДАННЫХ: на стенде с потолком +1 проходит лишний запуск;
  * счёт по ЗАДАЧЕ, а не по сессии: смена wip — счёт новой задачи с нуля;
  * чужие строки журнала (решения agent-level.py) запуском не считаются;
  * дверь: короткая причина не открывает; открытая — проход И строка журнала
    с причиной; дверь одной задачи не открывает другую;
  * запуск, отменённый соседом-сторожем скила, не засчитан;
  * пачка одновременных запусков не проскакивает потолок (замок, три пачки);
  * нет wip / две wip / только трейлер в истории — запуски считаются по СЕССИИ
    под потолком «без-задачи» (раньше — проход со следом «задача не определена»);
    на настоящем журнале вызовы-агентов.jsonl: сессия, чьи запуски легли
    skipped, упирается в потолок;
  * глубина: помощник глубины 2 зовёт третьего — отказ; глубина 2 тяжёлого типа —
    отказ с подсказкой harness-worker; глубина берётся из meta.json помощника
    (spawnDepth) по agent_id, нет файла — глубина неизвестна, правило молчит;
  * запуски вложенных помощников управляющего (agent_type harness-lead) считаются
    отдельно на управляющего (ключ — его agent_id): «всего на управляющего» из
    pipeline.yaml → помощники.управляющий, «параллельно» из helper-roles.yaml
    (живые = дети с недавно писавшимся транскриптом); два управляющих не мешают
    друг другу, и смена с её потолком «без-задачи» тоже;
  * чужой инструмент хук не трогает;
  * тяжёлый тип помощника (без типа, general-purpose, claude — грузят CLAUDE.md
    проекта, 71 тыс. токенов на старт) получает ПРЕДУПРЕЖДЕНИЕ в additionalContext
    с именем harness-worker, а не отказ, и строку agent.heavy-type в журнале;
    лёгкие типы (harness-worker, Explore, Plan, claude-code-guide) — без
    предупреждения и без строки; запуск, отменённый сторожем скила, не считается.

Механизм берётся рядом с пробой либо из HARNESS_AGENT_BUDGET_GUARD (дверь для
мутаций: копия с поломкой). Прогон: python3 scripts/hooks/test_agent_budget_guard.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GUARD = Path(os.environ.get("HARNESS_AGENT_BUDGET_GUARD") or HERE / "agent_budget_guard.py")
PIPELINE = ROOT / "harness" / "config" / "pipeline.yaml"
JOURNAL_NAME = "вызовы-агентов.jsonl"
SRC = "scripts/hooks/agent_budget_guard.py"
LAUNCH_EVENT = "agent.launch"
HEAVY_EVENT = "agent.heavy-type"
LIGHT_TYPE = "harness-worker"
NO_TASK = "без-задачи"
LEAD_LEVEL = "управляющий"
LEAD_PARALLEL = 3
LEAD_TOTAL = 4
REAL_JOURNAL = Path(os.environ.get("HARNESS_REAL_AGENT_JOURNAL")
                    or "/var/log/harness") / JOURNAL_NAME

checked = failed = 0


def check(name: str, expected, actual) -> None:
    global checked, failed
    checked += 1
    if expected == actual:
        print(f"  ок    {name}")
    else:
        failed += 1
        print(f"  ПЛОХО {name}: ждали «{expected}», вышло «{actual}»")


def levels_from_data(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("помощники") or {}


class Stand:
    """Свой каталог журналов, своя карта и своя копия pipeline.yaml."""

    def __init__(self, base: Path, ceilings: dict):
        self.base = base
        self.logs = base / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.devmap = base / "dev-map.yaml"
        self.pipeline = base / "pipeline.yaml"
        self.pipeline.write_text(
            yaml.safe_dump({"помощники": ceilings}, allow_unicode=True), encoding="utf-8")
        self.roles = base / "helper-roles.yaml"
        self.roles.write_text(yaml.safe_dump(
            {"роли": {"harness-lead": {"параллельно_максимум": LEAD_PARALLEL}}},
            allow_unicode=True), encoding="utf-8")

    def set_map(self, cards: list) -> None:
        tree = {"lines": [{"id": "линия", "items": [
            {"id": tid, "status": status,
             **({"глубина": {"уровень": level}} if level else {})}
            for tid, status, level in cards]}]}
        self.devmap.write_text(yaml.safe_dump(tree, allow_unicode=True), encoding="utf-8")

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_TASK"}
        env.update(HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs),
                   HARNESS_DEVMAP=str(self.devmap),
                   HARNESS_EVENTS_STATE=str(self.base / "state"),
                   HARNESS_PIPELINE_CONF=str(self.pipeline),
                   HARNESS_HELPER_ROLES=str(self.roles),
                   PYTHONDONTWRITEBYTECODE="1")
        return env

    def launch(self, description: str = "помощник: код", tool: str = "Agent",
               session: str = "проба", subagent_type: str | None = None,
               caller_depth: int | None = None, lead: str | None = None):
        """caller_depth — spawnDepth зовущего помощника (None — зовёт смена);
        lead — agent_id управляющего-зовущего (глубина 1, agent_type harness-lead)."""
        tool_input = {"description": description, "prompt": "работа"}
        if subagent_type is not None:
            tool_input["subagent_type"] = subagent_type
        payload = {"tool_name": tool, "session_id": session, "tool_input": tool_input}
        if caller_depth is not None:
            payload.update(agent_id="глубина", transcript_path=str(self.base / "s.jsonl"))
            meta = self.base / session / "subagents" / "agent-глубина.meta.json"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps({"spawnDepth": caller_depth}), encoding="utf-8")
        if lead is not None:
            payload.update(agent_id=lead, agent_type="harness-lead",
                           transcript_path=str(self.base / "s.jsonl"))
            self.write_meta(session, lead, {"spawnDepth": 1})
        proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env(), timeout=60)
        return proc.returncode, proc.stderr

    def write_meta(self, session: str, agent: str, meta: dict) -> Path:
        path = self.base / session / "subagents" / f"agent-{agent}.meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta), encoding="utf-8")
        return path

    def child(self, lead: str, name: str, idle_s: int, session: str = "проба") -> None:
        """Исполнитель управляющего; idle_s — сколько секунд его транскрипт молчит."""
        meta = self.write_meta(session, name, {"spawnDepth": 2, "parentAgentId": lead})
        log = meta.with_name(f"agent-{name}.jsonl")
        log.write_text("{}\n", encoding="utf-8")
        stamp = time.time() - idle_s
        os.utime(log, (stamp, stamp))

    def launch_typed(self, subagent_type: str | None):
        """(код, текст для модели из additionalContext) запуска помощника типа."""
        tool_input = {"description": "помощник: код", "prompt": "работа"}
        if subagent_type is not None:
            tool_input["subagent_type"] = subagent_type
        payload = {"tool_name": "Agent", "session_id": "проба", "tool_input": tool_input}
        proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env(), timeout=60)
        try:
            out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError:
            out = {}
        context = ((out.get("hookSpecificOutput") or {}).get("additionalContext") or "")
        return proc.returncode, context

    def heavy_rows(self) -> list:
        return [r for r in self.rows()
                if r.get("src") == SRC and r.get("event") == HEAVY_EVENT]

    def door(self, reason: str) -> int:
        return subprocess.run([sys.executable, str(GUARD), "--door", reason],
                              capture_output=True, text=True, env=self.env(),
                              timeout=60).returncode

    def rows(self) -> list:
        path = self.logs / JOURNAL_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def launch_rows(self) -> list:
        return [r for r in self.rows()
                if r.get("src") == SRC and r.get("event") == LAUNCH_EVENT]


def judge_level(tmp: Path, level: str, total: int, review: int) -> None:
    stand = Stand(tmp / f"уровень-{level}", {level: {"всего": total, "ревью": review}})
    stand.set_map([("задача-а", "wip", level)])
    if total:
        codes = [stand.launch()[0] for _ in range(total)]
        check(f"{level}: {total} запуск(а) в пределах потолка проходят", [0] * total, codes)
        code, text = stand.launch()
        check(f"{level}: запуск №{total + 1} сверх потолка — отказ", 2, code)
        check(f"{level}: отказ называет потолок числом и дверь --door",
              True, str(total) in text and "--door" in text)
    else:
        codes = [stand.launch()[0] for _ in range(20)]
        check(f"{level}: потолок 0 — 20 запусков подряд проходят", [0] * 20, codes)

    if review:
        stand = Stand(tmp / f"ревью-{level}", {level: {"всего": 0, "ревью": review}})
        stand.set_map([("задача-р", "wip", level)])
        codes = [stand.launch(f"[ревью] круг {i}")[0] for i in range(review)]
        check(f"{level}: {review} ревьюер(а) проходят", [0] * review, codes)
        check(f"{level}: ревьюер №{review + 1} — отказ", 2, stand.launch("review: ещё один")[0])
        check(f"{level}: помощник не-ревьюер при исчерпанном ревью проходит",
              0, stand.launch("помощник: код")[0])

    if total:
        # Числа из ДАННЫХ: потолок, зашитый в код, на этом стенде отказал бы.
        stand = Stand(tmp / f"данные-{level}", {level: {"всего": total + 1, "ревью": 0}})
        stand.set_map([("задача-д", "wip", level)])
        codes = [stand.launch()[0] for _ in range(total + 1)]
        check(f"{level}: потолок {total + 1} из данных стенда — {total + 1} запусков проходят",
              [0] * (total + 1), codes)


def judge_counting(tmp: Path) -> None:
    stand = Stand(tmp / "счёт", {"мелкая": {"всего": 2, "ревью": 1}})
    stand.set_map([("задача-а", "wip", "мелкая")])
    # В том же журнале лежат решения agent-level.py и строки v1 других писателей
    # о той же задаче — запусками они не являются.
    foreign = [{"ts": "2026-10-28T18:51:56+00:00", "повод": "harness-code-review",
                "уровень": "максимум", "зову": True, "причина": "проба"},
               {"v": 1, "ts": "2026-10-28T18:51:56+00:00", "src": "scripts/agent-level.py",
                "event": LAUNCH_EVENT, "outcome": "ok", "task": "задача-а"},
               {"v": 1, "ts": "2026-10-28T18:51:56+00:00", "src": SRC,
                "event": "agent.called", "outcome": "ok", "task": "задача-а"}]
    (stand.logs / JOURNAL_NAME).write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in foreign * 2) + "\n",
        encoding="utf-8")
    check("строки agent-level.py не съедают потолок", 0, stand.launch()[0])
    check("второй запуск в пределах", 0, stand.launch()[0])
    check("третий — отказ (счёт ведётся по журналу)", 2, stand.launch()[0])
    rows = stand.launch_rows()
    check("каждый запуск оставил строку v1 с задачей", [(1, "задача-а")] * 3,
          [(r.get("v"), r.get("task")) for r in rows])
    check("отказ записан исходом refused", "refused", rows[-1].get("outcome") if rows else None)

    # Смена wip: счёт новой задачи с нуля — счёт по ВСЕЙ сессии здесь отказал бы.
    stand.set_map([("задача-а", "test", "мелкая"), ("задача-бэ", "wip", "мелкая")])
    check("новая wip — первый запуск проходит", 0, stand.launch()[0])
    check("новая wip — второй запуск проходит", 0, stand.launch()[0])

    # Дверь.
    check("дверь с короткой причиной не открывается", True, stand.door("надо") != 0)
    check("без двери третий запуск новой задачи — отказ", 2, stand.launch()[0])
    reason = "третий помощник: второй круг правок после ревью"
    check("дверь с причиной открывается", 0, stand.door(reason))
    check("через открытую дверь запуск проходит", 0, stand.launch()[0])
    last = stand.launch_rows()[-1]
    check("проход дверью записан с причиной", ("ok", reason),
          (last.get("outcome"), (last.get("data") or {}).get("дверь")))
    stand.set_map([("задача-бэ", "test", "мелкая"), ("задача-а", "wip", "мелкая")])
    check("дверь задачи-бэ не открывает исчерпанную задачу-а", 2, stand.launch()[0])


def judge_neighbour_refusal(tmp: Path) -> None:
    """Хуки на Agent идут ПАРАЛЛЕЛЬНО: запуск, отменённый сторожем скила, не
    состоялся и потолок есть не должен."""
    stand = Stand(tmp / "сосед", {"мелкая": {"всего": 2, "ревью": 1}})
    stand.set_map([("задача-с", "wip", "мелкая")])
    skills = stand.logs / "skills.jsonl"
    skills.write_text("", encoding="utf-8")
    for _ in range(3):
        stand.launch()
    check("запуски, отменённые сторожем скила, не засчитаны", 0,
          sum(1 for r in stand.launch_rows() if r.get("outcome") == "ok"))
    skills.write_text(json.dumps({"skill": "subagent-work", "источник": "хук",
                                  "сессия": "проба"}, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    check("после скила потолок целый: 2 прохода, затем отказ", [0, 0, 2],
          [stand.launch()[0] for _ in range(3)])


def judge_race(tmp: Path) -> None:
    """Помощников зовут пачкой в одном сообщении: хуки идут одновременно, и
    счёт «прочёл — дописал» без замка пропустил бы всю пачку. Одна пачка гонку
    не доказывает — три пачки по шесть."""
    results = []
    for round_no in range(3):
        stand = Stand(tmp / f"гонка-{round_no}", {"мелкая": {"всего": 2, "ревью": 0}})
        stand.set_map([("задача-г", "wip", "мелкая")])
        payload = json.dumps({"tool_name": "Agent", "session_id": "проба",
                              "tool_input": {"description": "помощник: код"}})
        procs = [subprocess.Popen([sys.executable, str(GUARD)], stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  text=True, env=stand.env()) for _ in range(6)]
        for proc in procs:
            proc.stdin.write(payload)
            proc.stdin.close()
        results.append(sum(1 for proc in procs if proc.wait(timeout=60) == 0))
    check("пачка из 6 одновременных запусков мелкой: проходят ровно 2 (×3)",
          [2, 2, 2], results)


def judge_fail_open(tmp: Path) -> None:
    stand = Stand(tmp / "открыто", {"мелкая": {"всего": 1, "ревью": 1},
                                    NO_TASK: {"всего": 2, "ревью": 1}})
    stand.set_map([("одна", "plan", "мелкая")])
    codes = [stand.launch()[0] for _ in range(3)]
    check("нет wip — счёт по сессии: два запуска проходят, третий отказан", [0, 0, 2], codes)
    rows = stand.launch_rows()
    check("запуск без задачи — исход ok, задача «-», сессия записана",
          ("ok", "-", "проба"),
          (rows[0].get("outcome"), rows[0].get("task"), rows[0].get("session")))
    check("чужая сессия без wip считает с нуля", 0, stand.launch(session="другая")[0])
    # Трейлер Dev-Map в истории называет ПРЕДЫДУЩУЮ задачу: её счёт чужой.
    git = ["git", "-C", str(stand.base), "-c", "user.name=проба", "-c", "user.email=p@p"]
    subprocess.run(git + ["init", "-q"], check=True, timeout=30)
    subprocess.run(git + ["add", "dev-map.yaml"], check=True, timeout=30)
    subprocess.run(git + ["commit", "-q", "-m", "проба\n\nDev-Map: прежняя-задача"],
                   check=True, timeout=30)
    stand.launch(session="третья")
    check("трейлер Dev-Map из истории задачей не считается", "-",
          (stand.launch_rows() or [{}])[-1].get("task"))
    stand.set_map([("раз", "wip", "мелкая"), ("два", "wip", "мелкая")])
    check("две wip — тоже счёт по сессии", [0, 2],
          [stand.launch(session="четвёртая")[0] for _ in range(1)]
          + [stand.launch(session="проба")[0]])
    stand = Stand(tmp / "без-потолка", {"мелкая": {"всего": 1, "ревью": 1}})
    stand.set_map([("одна", "plan", "мелкая")])
    check("нет потолка «без-задачи» в данных — проход со следом", [0, 0, 0],
          [stand.launch()[0] for _ in range(3)])
    check("след пропуска: потолок не определён", ("skipped", "потолок не определён"),
          ((stand.launch_rows() or [{}])[-1].get("outcome"),
           ((stand.launch_rows() or [{}])[-1].get("data") or {}).get("почему")))
    before = len(stand.rows())
    check("чужой инструмент пропускается", 0, stand.launch(tool="Bash")[0])
    check("чужой инструмент следа не оставляет", before, len(stand.rows()))


def judge_real_journal(tmp: Path) -> None:
    """Настоящий журнал: сессия, чьи запуски легли skipped «задача не определена»,
    не должна уходить от потолка. Список сессий берётся ПАРСИНГОМ журнала."""
    path = REAL_JOURNAL
    skipped: dict = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (row.get("src") == SRC and row.get("event") == LAUNCH_EVENT
                    and row.get("outcome") == "skipped" and row.get("task") == "-"
                    and (row.get("data") or {}).get("почему") == "задача не определена"):
                skipped[row.get("session") or ""] = skipped.get(row.get("session") or "", 0) + 1
    check("в настоящем журнале есть запуски без задачи (иначе проба слепа)", True,
          bool(skipped))
    if not skipped:
        return
    session, count = max(skipped.items(), key=lambda kv: kv[1])
    stand = Stand(tmp / "настоящий", {NO_TASK: {"всего": 3, "ревью": 0}})
    stand.set_map([("одна", "plan", "мелкая")])
    codes = [stand.launch(session=session)[0] for _ in range(count + 3)]
    check(f"сессия настоящего журнала ({count} запусков без задачи) упирается в потолок 3",
          [0, 0, 0] + [2] * count, codes)


def judge_depth(tmp: Path) -> None:
    """Глубина нового помощника = spawnDepth зовущего + 1 (смена — 0)."""
    stand = Stand(tmp / "глубина", {"мелкая": {"всего": 0, "ревью": 0}})
    stand.set_map([("задача-г", "wip", "мелкая")])
    check("смена зовёт общего помощника (глубина 1) — проход", 0,
          stand.launch(subagent_type="general-purpose")[0])
    check("помощник глубины 1 зовёт harness-worker (глубина 2) — проход", 0,
          stand.launch(subagent_type=LIGHT_TYPE, caller_depth=1)[0])
    for heavy in (None, "general-purpose", "claude"):
        code, text = stand.launch(subagent_type=heavy, caller_depth=1)
        check(f"глубина 2, тип {heavy or 'не задан'} — отказ с подсказкой {LIGHT_TYPE}",
              (2, True), (code, LIGHT_TYPE in text))
    for light in (LIGHT_TYPE, "Explore"):
        code, text = stand.launch(subagent_type=light, caller_depth=2)
        check(f"глубина 3, тип {light} — отказ (потолок глубины 2)", (2, True),
              (code, "глубин" in text))
    check("отказ по глубине записан исходом refused", "refused",
          (stand.launch_rows() or [{}])[-1].get("outcome"))
    ok_before = sum(1 for r in stand.launch_rows() if r.get("outcome") == "ok")
    stand.launch(subagent_type="general-purpose", caller_depth=1)
    check("отказ по глубине счёт задачи не увеличил", ok_before,
          sum(1 for r in stand.launch_rows() if r.get("outcome") == "ok"))
    payload = {"tool_name": "Agent", "session_id": "проба", "agent_id": "нет-файла",
               "transcript_path": str(stand.base / "s.jsonl"),
               "tool_input": {"description": "помощник", "subagent_type": "general-purpose"}}
    proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                          capture_output=True, text=True, env=stand.env(), timeout=60)
    check("meta.json нет — глубина неизвестна, правило молчит", 0, proc.returncode)


def judge_lead(tmp: Path) -> None:
    """Вложенные запуски управляющего: свой счёт, свой потолок, своя параллельность."""
    ceilings = {"без-задачи": {"всего": 2, "ревью": 1}, LEAD_LEVEL: {"всего": LEAD_TOTAL}}
    stand = Stand(tmp / "управляющий", ceilings)
    stand.set_map([])
    codes = [stand.launch(lead="Л1", subagent_type=LIGHT_TYPE)[0] for _ in range(LEAD_TOTAL)]
    check(f"управляющий: {LEAD_TOTAL} запусков проходят (потолок смены 2 их не касается)",
          [0] * LEAD_TOTAL, codes)
    code, text = stand.launch(lead="Л1", subagent_type=LIGHT_TYPE)
    check("управляющий: запуск сверх «всего на управляющего» — отказ с числом", (2, True),
          (code, f"{LEAD_TOTAL} из {LEAD_TOTAL}" in text))
    check("другой управляющий считается с нуля", 0,
          stand.launch(lead="Л2", subagent_type=LIGHT_TYPE)[0])
    check("смена считается как раньше: 2 запуска проходят", [0, 0],
          [stand.launch(subagent_type=LIGHT_TYPE)[0] for _ in range(2)])
    check("смена: третий запуск — отказ (запуски управляющих её не съели и не помогли)",
          2, stand.launch(subagent_type=LIGHT_TYPE)[0])
    check("смена с помощником не-управляющего глубины 1 считается как смена", 2,
          stand.launch(caller_depth=1, subagent_type=LIGHT_TYPE)[0])
    check("в строке запуска записан agent_id управляющего", True,
          any((r.get("data") or {}).get("управляющий") == "Л1" for r in stand.launch_rows()))

    stand = Stand(tmp / "параллельно", {LEAD_LEVEL: {"всего": 0}})
    stand.set_map([])
    for number in range(LEAD_PARALLEL):
        stand.child("Л1", f"живой{number}", idle_s=5)
    code, text = stand.launch(lead="Л1", subagent_type=LIGHT_TYPE)
    check(f"{LEAD_PARALLEL} живых исполнителей — четвёртый отказан (число из helper-roles)",
          (2, True), (code, "параллельно" in text))
    check("живые чужого управляющего не мешают", 0,
          stand.launch(lead="Л2", subagent_type=LIGHT_TYPE)[0])
    stand = Stand(tmp / "параллельно-стар", {LEAD_LEVEL: {"всего": 0}})
    stand.set_map([])
    for number in range(LEAD_PARALLEL + 2):
        stand.child("Л1", f"старый{number}", idle_s=3600)
    check("давно замолчавшие исполнители живыми не считаются", 0,
          stand.launch(lead="Л1", subagent_type=LIGHT_TYPE)[0])


def judge_type_hint(tmp: Path) -> None:
    """Тяжёлый тип помощника — предупреждение и счёт, не отказ."""
    stand = Stand(tmp / "тип", {"мелкая": {"всего": 0, "ревью": 0}})
    stand.set_map([("задача-т", "wip", "мелкая")])
    for heavy in (None, "general-purpose", "claude"):
        code, context = stand.launch_typed(heavy)
        check(f"тип {heavy or 'не задан'}: запуск не отказан", 0, code)
        check(f"тип {heavy or 'не задан'}: предупреждение называет {LIGHT_TYPE} и 71 тыс.",
              True, LIGHT_TYPE in context and "71" in context)
    rows = stand.heavy_rows()
    check("три тяжёлых запуска — три строки agent.heavy-type с задачей",
          [("задача-т", "ok")] * 3, [(r.get("task"), r.get("outcome")) for r in rows])
    check("в строке записан тип помощника", ["-", "general-purpose", "claude"],
          [(r.get("data") or {}).get("тип") for r in rows])
    for light in (LIGHT_TYPE, "Explore", "Plan", "claude-code-guide"):
        code, context = stand.launch_typed(light)
        check(f"тип {light}: без предупреждения", (0, ""), (code, context))
    check("лёгкие типы строк agent.heavy-type не добавили", 3, len(stand.heavy_rows()))
    check("строки agent.heavy-type потолок не едят: 7 запусков — 7 строк agent.launch",
          7, sum(1 for r in stand.launch_rows() if r.get("outcome") == "ok"))

    stand = Stand(tmp / "тип-сосед", {"мелкая": {"всего": 0, "ревью": 0}})
    stand.set_map([("задача-т", "wip", "мелкая")])
    (stand.logs / "skills.jsonl").write_text("", encoding="utf-8")
    code, context = stand.launch_typed("general-purpose")
    check("запуск, отменённый сторожем скила: ни предупреждения, ни строки",
          ("", 0), (context, len(stand.heavy_rows())))


def main() -> int:
    if not GUARD.exists():
        print(f"  ПЛОХО механизма нет: {GUARD}")
        print("SELFTEST: КРАСНЫЙ (механизма нет)")
        return 1
    levels = levels_from_data(PIPELINE)
    check("в pipeline.yaml есть уровни ключа «помощники»", True, bool(levels))
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for level, ceiling in levels.items():
            judge_level(tmp, str(level), int(ceiling.get("всего") or 0),
                        int(ceiling.get("ревью") or 0))
        judge_counting(tmp)
        judge_neighbour_refusal(tmp)
        judge_race(tmp)
        judge_fail_open(tmp)
        judge_real_journal(tmp)
        judge_depth(tmp)
        judge_lead(tmp)
        judge_type_hint(tmp)
    print(f"SELFTEST: {'зелёный' if not failed else 'КРАСНЫЙ'} ({checked} случаев, "
          f"уровней из данных {len(levels)}, неудач {failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
