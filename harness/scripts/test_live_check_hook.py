#!/usr/bin/env python3
"""Проба носителя живой проверки перехватчиков (класс B1).

Больной случай первым: хук, который НИЧЕГО не решил, не должен считаться
проверенным. Второй — хук, который решил, но своего следа в журнале не
оставил: «решение видно» и «решение записано» разные факты, и прибор обязан
различать их словами, а не одним кодом возврата.

Стенд свой: свои хуки, свой журнал, своё состояние — боевые файлы не трогаются
([[the-probe-played-on-production]]).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

КОРЕНЬ = pathlib.Path(__file__).resolve().parent.parent
ПРИБОР = pathlib.Path(os.environ.get("HARNESS_LIVE_CHECK_HOOK")
                      or КОРЕНЬ / "scripts" / "live-check-hook.py")

ХОРОШИХ = ПЛОХИХ = 0


def случай(имя: str, вышло, ждём) -> None:
    global ХОРОШИХ, ПЛОХИХ
    if вышло == ждём:
        ХОРОШИХ += 1
        print(f"  ок    {имя}")
    else:
        ПЛОХИХ += 1
        print(f"  ПЛОХО {имя}: вышло {вышло!r}, ждали {ждём!r}")


def прогон(двор: pathlib.Path, довод: list[str]) -> subprocess.CompletedProcess:
    среда = dict(os.environ,
                 HARNESS_LIVE_CHECK_REPO=str(двор),
                 HARNESS_LIVE_CHECK_STATE=str(двор / "состояние.yaml"),
                 HARNESS_LIVE_CHECK_HOOKS=str(двор / "хуки.yaml"),
                 HARNESS_LIVE_CHECK_HOOK_TRACE_DIR=str(двор / "следы"),
                 HARNESS_LOG_DIR=str(двор / "журналы"))
    return subprocess.run([sys.executable, str(ПРИБОР), *довод],
                          capture_output=True, text=True, timeout=120, env=среда)


with tempfile.TemporaryDirectory() as врем:
    двор = pathlib.Path(врем)
    (двор / "scripts" / "hooks").mkdir(parents=True)
    (двор / "журналы").mkdir()
    (двор / "состояние.yaml").write_text("проверено: {}\n", encoding="utf-8")

    # Хук, который ОТКАЗЫВАЕТ словами и пишет своё решение в журнал — такой же,
    # как живой сторож команд: решение на stderr, строка JSONL в журнал.
    отказчик = двор / "scripts" / "hooks" / "otkazchik.py"
    отказчик.write_text(
        "import json,os,sys\n"
        "sys.stdin.read()\n"
        "ж=os.path.join(os.environ['HARNESS_LOG_DIR'],'стенд.jsonl')\n"
        "open(ж,'a',encoding='utf-8').write(json.dumps({'verdict':'deny'})+'\\n')\n"
        "print('команда отменена: образ не пересобирается', file=sys.stderr)\n"
        "sys.exit(2)\n", encoding="utf-8")
    # Хук, который молчит и пропускает: решения не видно вовсе.
    молчун = двор / "scripts" / "hooks" / "molchun.py"
    молчун.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
    # Хук, который отказывает словами, но следа не пишет.
    беспамятный = двор / "scripts" / "hooks" / "bespamyatnyj.py"
    беспамятный.write_text(
        "import sys\nsys.stdin.read()\n"
        "print('команда отменена: образ не пересобирается', file=sys.stderr)\n"
        "sys.exit(2)\n", encoding="utf-8")

    данные = {"хуки": {
        "scripts/hooks/otkazchik.py": {
            "событие": json.dumps({"tool_name": "Bash",
                                   "tool_input": {"command": "docker compose restart api"}}),
            "ждём_в_решении": "образ не пересобирается",
            "журнал": "стенд.jsonl"},
        "scripts/hooks/molchun.py": {
            "событие": json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
            "ждём_в_решении": "образ не пересобирается"},
        "scripts/hooks/bespamyatnyj.py": {
            "событие": json.dumps({"tool_name": "Bash",
                                   "tool_input": {"command": "docker compose restart api"}}),
            "ждём_в_решении": "образ не пересобирается",
            "журнал": "стенд.jsonl"},
    }}
    (двор / "хуки.yaml").write_text(
        yaml.dump(данные, allow_unicode=True, sort_keys=False), encoding="utf-8")

    print("── БОЛЬНОЙ СЛУЧАЙ: хук промолчал — решения не видно ──")
    готово = прогон(двор, ["scripts/hooks/molchun.py"])
    случай("молчащий хук не проходит", готово.returncode, 1)
    случай("и причина названа СВОИМ словом носителя (код 1 даёт и падение)",
           "решения не видно" in (готово.stdout + готово.stderr), True)

    print("── БОЛЬНОЙ СЛУЧАЙ: решение есть, а своего следа хук не оставил ──")
    готово = прогон(двор, ["scripts/hooks/bespamyatnyj.py"])
    случай("хук без записи в свой журнал не проходит", готово.returncode, 1)
    случай("и причина названа отдельно от молчания — «журнал не вырос»",
           "не вырос" in (готово.stdout + готово.stderr), True)

    print("── ЗДОРОВЫЙ: решение видно и записано ──")
    готово = прогон(двор, ["scripts/hooks/otkazchik.py"])
    случай("хук, отказавший словами и записавший строку, доказан",
           готово.returncode, 0)
    след = двор / "следы" / "scripts-hooks-otkazchik.py.log"
    случай("след написан прибором", след.is_file(), True)
    текст = след.read_text(encoding="utf-8") if след.is_file() else ""
    случай("в следе чужой текст решения",
           "образ не пересобирается" in текст, True)
    случай("и разделитель, по которому гейт узнаёт след",
           "--- решение хука ---" in текст, True)
    случай("без «--записать» состояние не меняется",
           yaml.safe_load((двор / "состояние.yaml").read_text(encoding="utf-8")),
           {"проверено": {}})

    print("── ЗАПИСЬ: --записать кладёт исход в состояние ──")
    прогон(двор, ["scripts/hooks/otkazchik.py", "--записать"])
    состояние = yaml.safe_load((двор / "состояние.yaml").read_text(encoding="utf-8"))
    запись = (состояние.get("проверено") or {}).get("scripts/hooks/otkazchik.py") or {}
    случай("исход записан", запись.get("исход"), "ок")
    случай("цитата — чужой текст решения",
           "образ не пересобирается" in str(запись.get("увидели")), True)
    случай("след назван путём", bool(запись.get("след")), True)

    print("── БОЛЬНОЙ СЛУЧАЙ: событие в данных не названо ──")
    (двор / "хуки.yaml").write_text(
        yaml.dump({"хуки": {"scripts/hooks/otkazchik.py": {"ждём_в_решении": "что-то"}}},
                  allow_unicode=True, sort_keys=False), encoding="utf-8")
    готово = прогон(двор, ["scripts/hooks/otkazchik.py"])
    случай("без события прибор отказывает, а не придумывает его",
           готово.returncode, 1)
    случай("и говорит, чего не хватает",
           "событи" in (готово.stdout + готово.stderr).lower(), True)

print(f"\nНОСИТЕЛЬ ХУКОВ: хороших {ХОРОШИХ}, плохих {ПЛОХИХ}")
sys.exit(1 if ПЛОХИХ else 0)
