#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проба сторожа модели: важный шаг чужой моделью не делается.

Откуда взята. Владелец 18.10.2026: «получается смена сонет, а нужные шаги
нужными моделями. мы можем добиться того, чтобы это не скилами, а строго кодом
контролировалось? чтобы сонет случайно важный шаг сам не сделал, типа
написания спеки или её ревью, а строго опусом. чтобы та таблица очень строго
выполнялась».

Что делало это возможным. Раскладка «работа → модель» была, но исполнял её
ТОЛЬКО хук вызова скила: позвали скил — переключил модель. Шаг без скила
(написать спеку руками, дописать пробу) шёл на какой угодно модели, и замер
18.10 показал 100 % шагов на opus именно потому, что смена всегда поднималась
опусом — в обратную сторону правило не держало ничего.

Признак шага берётся из АРТЕФАКТА (путь файла), а не из вызова скила: скил
можно забыть позвать, а файл спеки не подделать.

Прогон: python3 scripts/hooks/test_model_guard.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
СТОРОЖ = КОРЕНЬ / "scripts" / "hooks" / "model_guard.py"

путей = 0
плохо = 0

# Поля входа хука у помощника. У самой смены их во входе нет.
ПРИЗНАК_ПОМОЩНИКА = {"agent_id": "aff39fafd03d5a3d3", "agent_type": "general-purpose"}
# Параллельный помощник той же сессии: его транскрипт лежит рядом и свежее.
СОСЕД = "a0000000000000000"


def случай(имя: str, ждём, вышло) -> None:
    global путей, плохо
    путей += 1
    if ждём == вышло:
        print(f"  ок    {имя}")
    else:
        плохо += 1
        print(f"  ПЛОХО {имя}: ждали «{ждём}», вышло «{вышло}»")


def отказал(код: int, текст: str) -> bool:
    """Отказ СТОРОЖА, а не «нет такого файла»: python на отсутствующем скрипте
    тоже отвечает кодом 2, и пять случаев пробы были зелены ни от чего."""
    return код == 2 and "модель" in текст.lower()


СЕССИЯ = "проба-сессия"


def стенд(врем: str, модель: str, сам: bool, модель_смены: str | None = None,
          файл_помощника: bool = True) -> dict:
    """Транскрипты живой формы и поля входа, которые к ним ведут.

    Транскрипт смены — `<каталог>/<session_id>.jsonl`, помощника —
    `<каталог>/<session_id>/subagents/agent-<agent_id>.jsonl`. `модель` —
    модель ТОГО, кто пишет; `модель_смены` — модель смены, если она другая.
    Рядом всегда лежит СВЕЖИЙ транскрипт соседа с другой моделью: помощники
    идут параллельно, и сторож обязан взять свой файл, а не любой.
    """
    def строка(м: str, побочная: bool) -> str:
        return json.dumps({"type": "assistant", "isSidechain": побочная,
                           "message": {"model": м, "usage": {}}},
                          ensure_ascii=False) + "\n"
    смена = Path(врем) / f"{СЕССИЯ}.jsonl"
    смена.write_text(строка(модель_смены or модель, False), encoding="utf-8")
    поля = {"transcript_path": str(смена), "session_id": СЕССИЯ}
    if сам:
        return поля
    каталог = Path(врем) / СЕССИЯ / "subagents"
    каталог.mkdir(parents=True)
    if файл_помощника:
        свой = каталог / f"agent-{ПРИЗНАК_ПОМОЩНИКА['agent_id']}.jsonl"
        свой.write_text(строка(модель, True), encoding="utf-8")
        os.utime(свой, (1_000_000_000, 1_000_000_000))
    чужая = "claude-sonnet-5-5" if род(модель) == "opus" else "claude-opus-5-5"
    (каталог / f"agent-{СОСЕД}.jsonl").write_text(строка(чужая, True), encoding="utf-8")
    return {**поля, **ПРИЗНАК_ПОМОЩНИКА}


def род(имя: str) -> str:
    return next((р for р in ("opus", "sonnet", "haiku") if р in имя), имя)


def позвать(путь_файла: str, модель: str, среда: dict | None = None,
            сам: bool = False, модель_смены: str | None = None,
            файл_помощника: bool = True, журнал: list | None = None,
            поля: dict | None = None) -> tuple[int, str]:
    """Зовёт сторожа так, как его зовёт оболочка: JSON на stdin.

    `сам=True` — пишет САМА смена, иначе помощник. У помощника оболочка
    кладёт во вход `agent_id` и `agent_type`, а `transcript_path` у обоих —
    транскрипт ОСНОВНОЙ сессии, isSidechain=false. `поля` дописываются во вход.
    """
    with tempfile.TemporaryDirectory() as врем:
        вход = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": путь_файла, "content": "текст"},
            **стенд(врем, модель, сам, модель_смены, файл_помощника),
            **(поля or {}),
        }, ensure_ascii=False)
        # Свой LOG_DIR: иначе открытая живая дверь сторожа красит каждый отказ.
        окружение = {"PATH": "/usr/bin:/bin", "HOME": врем, "LC_ALL": "C.UTF-8",
                     "LOG_DIR": врем}
        окружение.update(среда or {})
        готово = subprocess.run([sys.executable, str(СТОРОЖ)], input=вход,
                                capture_output=True, text=True, env=окружение)
        if журнал is not None:
            след = Path(врем) / "model-guard.jsonl"
            журнал.extend(след.read_text(encoding="utf-8").splitlines() if след.exists() else [])
        return готово.returncode, (готово.stderr or "") + (готово.stdout or "")


print("── БОЛЬНОЙ СЛУЧАЙ 18.10.2026: важный шаг чужой моделью ──")
код, текст = позвать("docs/specs/2026-10-18-проба.md", "claude-sonnet-5-5")
случай("спеку пишет sonnet — отказ своими словами", True, отказал(код, текст))
случай("отказ называет и нужную модель, и нынешнюю",
       True, "opus" in текст and "sonnet" in текст)
случай("отказ называет законный ход — переключение модели",
       True, "model.sh" in текст)

код, текст = позвать("docs/plans/задача.md", "claude-sonnet-5-5")
случай("план пишет sonnet — отказ", True, отказал(код, текст))
код, текст = позвать(".tasks/задача/pretask.yaml", "claude-sonnet-5-5")
случай("контракт пишет sonnet — отказ", True, отказал(код, текст))
код, текст = позвать("scripts/test_chto_to.py", "claude-sonnet-5-5")
случай("пробу пишет sonnet — отказ", True, отказал(код, текст))

print("── ЗДОРОВАЯ ПОЛОВИНА: работа не остановлена ──")
код, _ = позвать("docs/specs/2026-10-18-проба.md", "claude-opus-5")
случай("спеку пишет opus — пропускает", 0, код)
код, _ = позвать("scripts/model_guard.py", "claude-sonnet-5-5")
случай("обычный код пишет sonnet — пропускает", 0, код)
код, _ = позвать("STATE.md", "claude-sonnet-5-5")
случай("состояние смены пишет sonnet — пропускает", 0, код)
код, _ = позвать("docs/specs/2026-10-18-проба.md", "claude-opus-5-5")
случай("старшая модель того же рода подходит", 0, код)

print("── КТО ДЕЛАЕТ ШАГ: труба — помощнику, смена оркестрирует ──")
# Владелец 18.10.2026: «сессия в первую очередь должна работать как
# оркестратор <…> но шаги трубы делают помощники, а смена ими управляет».
код, текст = позвать("scripts/novyj_pribor.py", "claude-sonnet-5-5", сам=True)
случай("код пишет САМА смена — отказ своими словами", True, отказал(код, текст))
случай("отказ зовёт помощника", True, "помощник" in текст.lower())
код, _ = позвать("scripts/novyj_pribor.py", "claude-sonnet-5-5", сам=False)
случай("тот же код пишет помощник — пропускает", 0, код)
код, _ = позвать("STATE.md", "claude-sonnet-5-5", сам=True)
случай("состояние смены пишет сама смена — это её работа, пропускает", 0, код)
код, _ = позвать("dev-map.yaml", "claude-sonnet-5-5", сам=True)
случай("карту задач ведёт сама смена — пропускает", 0, код)

print("── МОДЕЛЬ ПОМОЩНИКА: из ЕГО транскрипта, не из транскрипта смены ──")
# Смена на sonnet, помощник на opus: модель смены во входе помощника — чужая.
код, текст = позвать(".tasks/задача/review/generalist.yaml", "claude-opus-5-5",
                     модель_смены="claude-sonnet-5-5")
случай("смена sonnet, помощник opus пишет ревью — пропускает", 0, код)
код, текст = позвать(".tasks/задача/review/generalist.yaml", "claude-sonnet-5-5",
                     модель_смены="claude-opus-5-5")
случай("смена opus, помощник sonnet пишет ревью — отказ по модели", True,
       отказал(код, текст) and "sonnet" in текст and "помощник" not in текст.lower())
след: list = []
код, _ = позвать(".tasks/задача/review/generalist.yaml", "claude-opus-5-5",
                 модель_смены="claude-sonnet-5-5", файл_помощника=False, журнал=след)
случай("файла помощника нет — не модель смены, а «модель не прочитана»",
       (0, True), (код, any("модель не прочитана" in с for с in след)))

# Основная сессия, поднятая с `--agent`, несёт agent_type, но не agent_id.
код, текст = позвать(".tasks/задача/review/generalist.yaml", "claude-opus-5-5", сам=True,
                     поля={"agent_type": "general-purpose"})
случай("agent_type без agent_id — это смена, отказ по «кто»", True,
       отказал(код, текст) and "помощник" in текст.lower())

print("── ВТОРОЙ ПУТЬ ЗАПИСИ: через оболочку ──")
# Правило, закрытое на одном пути из двух, обходят, а не исполняют
# ([[a-rule-covering-one-path-of-two]]): файлы пишутся и инструментом записи,
# и командой оболочки — у этой смены вторым способом пишется почти всё.
def позвать_bash(команда: str, модель: str, сам: bool = True) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as врем:
        вход = json.dumps({"tool_name": "Bash", "tool_input": {"command": команда},
                           **стенд(врем, модель, сам)}, ensure_ascii=False)
        готово = subprocess.run([sys.executable, str(СТОРОЖ)], input=вход,
                                capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin", "HOME": врем, "LC_ALL": "C.UTF-8",
                                     "LOG_DIR": врем})
        return готово.returncode, (готово.stderr or "") + (готово.stdout or "")

код, текст = позвать_bash("cat > docs/specs/2026-10-18-обход.md <<'EOF'\nтекст\nEOF", "claude-sonnet-5-5")
случай("спека пишется оболочкой из смены — отказ", True, отказал(код, текст))
код, текст = позвать_bash("sed -i 's/a/b/' scripts/model-for.sh", "claude-sonnet-5-5")
случай("правка кода через sed -i из смены — отказ", True, отказал(код, текст))
код, _ = позвать_bash("cat docs/specs/2026-10-18-обход.md", "claude-sonnet-5-5")
случай("ЧТЕНИЕ того же файла оболочкой — пропускает", 0, код)
код, _ = позвать_bash("python3 scripts/model-for.sh спека", "claude-sonnet-5-5")
случай("запуск кода, а не запись в него — пропускает", 0, код)
# БОЛЬНОЙ СЛУЧАЙ сторожа: «2>&1» — не запись в файл, а перенаправление ошибок.
# Прямолинейный признак «в команде есть > и путь» отменял бы КАЖДЫЙ прогон
# проверки, и сторожа отключили бы в первый же час.
код, текст = позвать_bash("python3 scripts/check-registry.py 2>&1 | tail -2", "claude-sonnet-5-5")
случай("прогон с «2>&1» — не запись, пропускает", 0, код)
код, _ = позвать_bash("grep -n ПЛОХО scripts/hooks/test_model_guard.py", "claude-sonnet-5-5")
случай("поиск по файлу пробы — пропускает", 0, код)
код, _ = позвать_bash("bash scripts/gates.sh > /var/log/harness/ворота.log 2>&1", "claude-sonnet-5-5")
случай("вывод прогона в журнал — пропускает", 0, код)
код, _ = позвать_bash("cat > STATE.md <<'EOF'\nтекст\nEOF", "claude-sonnet-5-5")
случай("состояние смены пишется оболочкой — это работа смены, пропускает", 0, код)

print("── ЖИВАЯ ФОРМА ВХОДА: помощника видно по входу, не по транскрипту ──")
# Транскрипт во входе помощника — ОСНОВНОЙ сессии, и в нём isSidechain=false:
# суждение по транскрипту принимало каждого помощника за смену.
код, текст = позвать(".tasks/задача/review/generalist.yaml", "claude-opus-5-5", сам=False)
случай("помощник с agent_id пишет ревью — пропускает", 0, код)
код, текст = позвать(".tasks/задача/review/generalist.yaml", "claude-opus-5-5", сам=True)
случай("тот же вход без agent_id — это смена, отказ", True,
       отказал(код, текст) and "помощник" in текст.lower())
код, _ = позвать_bash("sed -i 's/a/b/' scripts/model-for.sh", "claude-sonnet-5-5", сам=False)
случай("помощник с agent_id правит код оболочкой — пропускает", 0, код)

print("── ЗАКОННАЯ ДВЕРЬ И ОТКАЗЫ САМОГО СТОРОЖА ──")
код, текст = позвать("docs/specs/проба.md", "claude-sonnet-5-5",
                     {"HARNESS_MODEL_GUARD_WHY": "лимит на opus, работа встанет совсем"})
случай("дверь с названной причиной — пропускает", 0, код)
код, текст = позвать("docs/specs/проба.md", "claude-sonnet-5-5",
                     {"HARNESS_MODEL_GUARD_WHY": "  "})
случай("дверь без причины не открывается", True, отказал(код, текст))

# Сторож, который не смог узнать модель, НЕ запрещает работу: он не знает, с
# чем сравнивать, и молчаливый запрет остановил бы смену на ровном месте.
with tempfile.TemporaryDirectory() as врем:
    вход = json.dumps({"tool_name": "Write", "transcript_path": f"{врем}/нет-такого.jsonl",
                       "tool_input": {"file_path": "docs/specs/проба.md", "content": "т"},
                       **ПРИЗНАК_ПОМОЩНИКА},
                      ensure_ascii=False)
    готово = subprocess.run([sys.executable, str(СТОРОЖ)], input=вход,
                            capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "HOME": врем,
                                 "LC_ALL": "C.UTF-8", "LOG_DIR": врем})
    случай("модель не прочитана — работа идёт, но след остаётся", 0, готово.returncode)

готово = subprocess.run([sys.executable, str(СТОРОЖ)], input="не json",
                        capture_output=True, text=True)
случай("битый вход не останавливает работу", 0, готово.returncode)

print("── ДВЕРЬ, КОТОРУЮ ХУК МОЖЕТ УВИДЕТЬ ──")
# БОЛЬНОЙ СЛУЧАЙ 18.10.2026, пойман через минуту после включения: дверью была
# переменная окружения КОМАНДЫ, а хук запускает оболочка — её окружение другое,
# и дверь не открывалась НИКОГДА. Правило без достижимой двери обходят,
# отключая правило ([[a-guard-without-a-legal-door]]).
def с_дверью(путь_файла: str, модель: str, причина: str, минут: int = 15) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as врем:
        дверь = Path(врем) / "model-guard.дверь"
        дверь.write_text(json.dumps(
            {"почему": причина, "до": int(__import__("time").time()) + минут * 60},
            ensure_ascii=False), encoding="utf-8")
        транскрипт = Path(врем) / "с.jsonl"
        транскрипт.write_text(json.dumps(
            {"type": "assistant", "isSidechain": False,
             "message": {"model": модель, "usage": {}}}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        вход = json.dumps({"tool_name": "Write", "transcript_path": str(транскрипт),
                           "tool_input": {"file_path": путь_файла, "content": "т"}},
                          ensure_ascii=False)
        готово = subprocess.run([sys.executable, str(СТОРОЖ)], input=вход,
                                capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin", "HOME": врем,
                                     "LC_ALL": "C.UTF-8", "LOG_DIR": врем})
        return готово.returncode, (готово.stderr or "") + (готово.stdout or "")

код, _ = с_дверью("docs/specs/проба.md", "claude-sonnet-5-5",
                  "лимит на opus, работа встанет совсем — чиню сторожа")
случай("дверь ФАЙЛОМ с причиной — пропускает", 0, код)
код, текст = с_дверью("docs/specs/проба.md", "claude-sonnet-5-5", "да")
случай("дверь файлом с отпиской — отказ", True, отказал(код, текст))
код, текст = с_дверью("docs/specs/проба.md", "claude-sonnet-5-5",
                      "причина есть, но дверь просрочена давно", минут=-5)
случай("просроченная дверь не открывает", True, отказал(код, текст))

print(f"\nСТОРОЖ МОДЕЛИ: путей {путей}, неудач {плохо}")
sys.exit(1 if плохо else 0)
