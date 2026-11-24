#!/usr/bin/env python3
"""П4 (ротация с преемником): судья скорости при ротации с преемником.

Спека docs/specs/2026-11-02-ротация-ждёт-помощников.md, раздел 14.9. Владелец
21.11: «скорость сторожи тормозит всю работу на серверах постоянно снижая
скорость… скорость работы — максимальная, сторожа её не снижают». Блокер «ротация»
замораживал потолок на всё время ожидания помощников (≈22 ч из 55). Теперь он
истинен только на этапах ждёт/идёт (минуты передачи); на этапе «дорабатывает»
блокера нет, а в работе считаются помощники ОБЕИХ смен.

Блокер «ротация» режет запуск в трёх местах — все через facts() судьи: потолок
задач (эффективный режим), потолок живых (ceiling_alive → launch-plan.py →
launch_plan_guard.py) и добор сторожа (warden_line). Поэтому проба держит и
устройство: в цепочке скорости флаг ротации читает ТОЛЬКО facts() судьи; второй
читатель (agent_budget_guard, launch-plan, launch_plan_guard) обошёл бы этапы
(спека, 14.13).

Судья — из живого файла (дверь SPEED_MODE_BIN), зовётся в дочернем процессе со
стендом в окружении (как Stand.env() в test_speed_mode.py). Итог — строка
«ПРОБА ЗЕЛЁНАЯ: …» / «ПРОБА КРАСНАЯ: …», код 0 / 1; стенд не годен — «ПРОБА
СЛОМАНА: …», код 2.
"""
from __future__ import annotations

import atexit
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def tree_file(*parts: str) -> Path:
    # Проект: scripts/ рядом с harness/; пакет: harness/scripts/ рядом с demons/.
    for candidate in (HERE.parent / "harness" / Path(*parts), HERE.parent / Path(*parts)):
        if candidate.is_file():
            return candidate
    return HERE.parent / "harness" / Path(*parts)


JUDGE = Path(os.environ.get("SPEED_MODE_BIN") or tree_file("demons", "speed_mode.py"))
# Остальные звенья цепочки скорости. Хуки: проект — scripts/hooks/, пакет — harness/hooks/.
HOOKS = HERE / "hooks" if (HERE / "hooks").is_dir() else HERE.parent / "hooks"
CHAIN = (HERE / "launch-plan.py", HOOKS / "launch_plan_guard.py", HOOKS / "agent_budget_guard.py")
FLAG_WORDS = re.compile(r"rotation_flag|rotation-waits-helpers|rotation-request")
OLD, NEW = "old-shift-0001", "new-shift-0002"
FLAG_NAME = "rotation-waits-helpers.json"
STRIP_ENV = ("SPEED_MODE", "CLAUDE_CODE_SESSION_ID", "HELPER_HUNG_MIN", "ROTATE_FLAG_STALE_MIN", "HARNESS_HOME")

CHILD = r"""
import importlib.util, json, sys, time
spec = importlib.util.spec_from_file_location("speed_mode_probe", sys.argv[1])
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)
what = sys.argv[2]
if what == "facts":
    print(json.dumps({"ротация": sm.facts()["ротация"]}, ensure_ascii=False))
else:
    found = sm.count_launches(time.time(), sm.load_data(), set())
    print(json.dumps({"calls": sorted(c for calls in found["tasks"].values() for c in calls),
                      "phantoms": found["phantoms"]}, ensure_ascii=False))
"""

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, got: str = "") -> None:
    checks.append((name, ok, got))
    print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": {got}"))


class Stand:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rot-successor-p4-"))
        atexit.register(shutil.rmtree, self.root, True)
        self.log, self.panel, self.project = self.root / "log", self.root / "panel", self.root / "proj"
        self.projects = self.root / "projects"
        self.transcripts = self.projects / str(self.project).replace("/", "-")
        for folder in (self.log, self.panel, self.project, self.transcripts, self.root / "hb"):
            folder.mkdir(parents=True, exist_ok=True)
        (self.root / "install.conf").write_text(
            f'LOG_DIR="{self.log}"\nSTATE_DIR="{self.root}"\nPANEL_STATE_DIR="{self.panel}"\n'
            f'HEARTBEAT_DIR="{self.root}/hb"\nPROJECT_DIR="{self.project}"\n', encoding="utf-8")
        (self.root / "harness.conf").write_text('SPEED_MODE="ultra"\nHELPER_HUNG_MIN="120"\n'
                                                'ROTATE_FLAG_STALE_MIN="30"\n', encoding="utf-8")
        (self.root / "dev-map.yaml").write_text("lines: []\n", encoding="utf-8")

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env.update({"HARNESS_INSTALL_CONF": str(self.root / "install.conf"),
                    "HARNESS_CONF": str(self.root / "harness.conf"),
                    "HARNESS_LOG_DIR": str(self.log), "LOG_DIR": str(self.log), "STATE_DIR": str(self.root),
                    "PANEL_STATE_DIR": str(self.panel), "HARNESS_PANEL_STATE": str(self.panel),
                    "HEARTBEAT_DIR": str(self.root / "hb"), "HARNESS_EVENTS_STATE": str(self.log / ".events"),
                    "HARNESS_DEVMAP": str(self.root / "dev-map.yaml"),
                    "CLAUDE_PROJECTS_DIR": str(self.projects), "PROJECT_DIR": str(self.project),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        return env

    def judge(self, what: str) -> dict:
        done = subprocess.run([sys.executable, "-c", CHILD, str(JUDGE), what], capture_output=True, text=True,
                              env=self.env(), timeout=120)
        if done.returncode != 0:
            raise RuntimeError(f"судья упал (код {done.returncode}): {done.stderr.strip()[-400:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])

    def put_flag(self, stage: str | None) -> None:
        path = self.panel / FLAG_NAME
        if stage is None:
            path.unlink(missing_ok=True)
            return
        now = int(time.time())
        path.write_text(json.dumps({
            "сессия": OLD, "этап": stage, "с": now - 600, "проверено": now, "ждали_сек": 0,
            "причина": "стенд П4", "помощников": 1, "принуждена": 0, "сказано": None,
            "преемник": NEW, "tmux": "agent-old-1795209000", "поручено": now - 900,
            "с_доработки": now - 300}, ensure_ascii=False), encoding="utf-8")

    def launch(self, call: str, session: str) -> None:
        """Строка agent.launch хука и живой помощник в каталоге ЕГО сессии (как Stand.launch пробы судьи)."""
        now = time.time()
        ts = datetime.datetime.fromtimestamp(now - 300, datetime.timezone.utc).isoformat()
        with open(self.log / "вызовы-агентов.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"v": 1, "ts": ts, "src": "scripts/hooks/agent_budget_guard.py",
                                 "event": "agent.launch", "outcome": "ok", "task": "-", "task_from": "arg",
                                 "session": session,
                                 "data": {"ревью": False, "описание": "проба П4", "тип": "harness-worker",
                                          "модель": "sonnet", "вызов": call, "часть": "Часть: hr.proba#1"}},
                                ensure_ascii=False) + "\n")
        folder = self.transcripts / session / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"agent-a{call}.meta.json").write_text(json.dumps({"toolUseId": call}), encoding="utf-8")
        (folder / f"agent-a{call}.jsonl").write_text("{}\n", encoding="utf-8")


def main() -> int:
    if not JUDGE.is_file():
        print(f"ПРОБА СЛОМАНА: П4 — судьи нет по пути {JUDGE}")
        return 2
    stand = Stand()
    try:
        # Стенд годен, только если флаг «идёт» блокер держит, а без флага — нет: иначе
        # красное «дорабатывает» значило бы, что судья не видит флага стенда вовсе.
        stand.put_flag(None)
        no_flag = stand.judge("facts")["ротация"]
        stand.put_flag("идёт")
        going = stand.judge("facts")["ротация"]
        if no_flag is not False or going is not True:
            print(f"ПРОБА СЛОМАНА: П4 — флаг стенда не читается (без флага {no_flag}, «идёт» {going})")
            return 2
        print("── блокер «ротация» по этапам ──")
        check("этап «идёт» — блокер держит (минуты передачи)", going is True, str(going))
        stand.put_flag("ждёт")
        waits = stand.judge("facts")["ротация"]
        check("этап «ждёт» — блокер держит", waits is True, str(waits))
        stand.put_flag("дорабатывает")
        finishing = stand.judge("facts")["ротация"]
        check("этап «дорабатывает» — блокера нет (преемник берёт задачи)", finishing is False, str(finishing))

        print("── в работе: помощники обеих смен ──")
        stand.launch("toolu_old_1", OLD)
        stand.launch("toolu_new_1", NEW)
        found = stand.judge("launches")
        check("живых запусков 2: старая смена и преемник", found["calls"] == ["toolu_new_1", "toolu_old_1"],
              json.dumps(found, ensure_ascii=False))
    except RuntimeError as беда:
        print(f"ПРОБА СЛОМАНА: П4 — {беда}")
        return 2

    print("── цепочка скорости: флаг читает только facts() судьи ──")
    missing = [str(p) for p in CHAIN if not p.is_file()]
    if missing:
        print(f"ПРОБА СЛОМАНА: П4 — звеньев цепочки нет: {', '.join(missing)}")
        return 2
    for link in CHAIN:
        hits = [n for n, line in enumerate(link.read_text(encoding="utf-8").splitlines(), 1) if FLAG_WORDS.search(line)]
        check(f"{link.name} флага ротации сам не читает", not hits, f"строки {hits}")
    judge_text = JUDGE.read_text(encoding="utf-8")
    facts_body = re.search(r"^def facts\(.*?(?=^def |\Z)", judge_text, re.S | re.M)
    outside = [w for w in FLAG_WORDS.findall(judge_text.replace(facts_body.group(0), "") if facts_body else "-")]
    # Вне facts() законно только подключение модуля (`import rotation_flag`).
    check("speed_mode.py читает флаг только в facts()",
          facts_body is not None and outside == ["rotation_flag"], f"вне facts(): {outside}")

    failed = [c for c in checks if not c[1]]
    if failed:
        print(f"ПРОБА КРАСНАЯ: П4 судья скорости — неудач {len(failed)} из {len(checks)}; первая: {failed[0][0]}")
        return 1
    print(f"ПРОБА ЗЕЛЁНАЯ: П4 судья скорости — путей {len(checks)}, «дорабатывает» скорость не режет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
