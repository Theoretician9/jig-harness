#!/usr/bin/env python3
"""Проба: приборы зовут модель через носитель, класс «прибор».

Пути 13 и 14 спеки hr.standards-and-policy (§4.4): ask-the-model.py и живая проба
замка agent-lock-check.py звали `claude -p` напрямую — модель приходила без
личности, и правила харнеса до неё не доходили. После правки первый элемент
вызова — `bash <корень>/scripts/claude-run.sh --класс прибор`, а замок
`--permission-mode manual` у ask-the-model.py и `--strict-mcp-config` у обоих
остаются на месте.

Судится ПОВЕДЕНИЕ: subprocess.run модуля подменяется записью доводов, модуль
зовёт свою функцию как в бою; модели нет, сети нет. Корень носителя — каталог
самого модуля (носитель лежит рядом с ним в обеих раскладках).

Двери мутаций: ASK_MODEL_UNDER_TEST, AGENT_LOCK_CHECK_UNDER_TEST — пути к копиям.

    python3 scripts/test_instrument_carrier.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASK = Path(os.environ.get("ASK_MODEL_UNDER_TEST") or HERE / "ask-the-model.py")
LOCK = Path(os.environ.get("AGENT_LOCK_CHECK_UNDER_TEST") or HERE / "agent-lock-check.py")

failed = 0
total = 0


def case(name, ok, detail=""):
    global failed, total
    total += 1
    failed += not ok
    print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": {detail}"))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recorder:
    """Подмена subprocess.run: пишет доводы, отвечает как модель без ошибки."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        out = json.dumps({"result": '{"ответ": 1}', "is_error": False})
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


def judge_call(who, argv, carrier, need_lock):
    """Первый вызов модели: bash <носитель> --класс прибор …"""
    case(f"{who}: модель позвана", bool(argv), "вызовов не было")
    if not argv:
        return
    case(f"{who}: первый элемент — bash, второй — {carrier}",
         argv[:2] == ["bash", str(carrier)], f"вызов {argv[:4]}")
    case(f"{who}: класс «прибор» сразу за носителем",
         argv[2:4] == ["--класс", "прибор"], f"вызов {argv[:5]}")
    case(f"{who}: голого claude в вызове нет",
         "claude" not in argv and not any(a.endswith("/claude") for a in argv), f"вызов {argv[:5]}")
    case(f"{who}: -p на месте", "-p" in argv, f"вызов {argv[:8]}")
    case(f"{who}: --strict-mcp-config на месте (носитель его не добавляет)",
         "--strict-mcp-config" in argv, f"вызов {argv[:10]}")
    if need_lock:
        pairs = list(zip(argv, argv[1:]))
        case(f"{who}: замок --permission-mode manual на месте",
             ("--permission-mode", "manual") in pairs, f"вызов {argv[:12]}")


def main() -> int:
    for path in (ASK, LOCK):
        if not path.is_file():
            print(f"ПРОБА СЛОМАНА: нет {path}")
            return 1

    carrier = ASK.resolve().parent / "claude-run.sh"
    case("носитель лежит рядом с прибором", carrier.is_file(), f"нет {carrier}")

    ask = load(ASK, "ask_the_model")
    rec = Recorder()
    ask.subprocess.run = rec
    ask.модель_для = lambda вид, корень: "haiku"
    with tempfile.TemporaryDirectory(prefix="sap-f2-ask-") as root:
        ask.спросить("вопрос пробы", {"ответ": "число"}, "дежурный", 30, Path(root))
    judge_call("ask-the-model.py", rec.calls[0] if rec.calls else [], carrier, need_lock=True)

    lock = load(LOCK, "agent_lock_check")
    rec = Recorder()
    lock.subprocess.run = rec
    lock.проба()
    carrier = LOCK.resolve().parent / "claude-run.sh"
    judge_call("agent-lock-check.py", rec.calls[0] if rec.calls else [], carrier, need_lock=False)
    case("agent-lock-check.py: оба вызова пробы замка через носитель",
         len(rec.calls) >= 1 and all(c[:4] == ["bash", str(carrier), "--класс", "прибор"]
                                     for c in rec.calls),
         f"вызовов {len(rec.calls)}: {[c[:2] for c in rec.calls]}")

    print(f"\nитог test_instrument_carrier: случаев {total}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
