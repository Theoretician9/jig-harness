#!/usr/bin/env python3
"""Проба: лимит работающего аккаунта — из потока CLI, а не прямым запросом к API.

Владелец 16.11.2026: прямой POST api.anthropic.com ключом подписки мимо Claude
Code — убрать. CLI сам пишет в поток stream-json событие rate_limit_event
(статус, окно, срок сброса, загрузка окон 5 ч и 7 дн), но не называет, чей это
ключ. Поэтому claude-run.sh кладёт в начало потока метку аккаунта (отпечаток
ключа или «login»), сторож входа читает последнее событие этой метки, а
account-switch.py судит по нему текущий аккаунт; прямой запрос остаётся там,
где данных нет (кандидаты, поток не свеж).

Судятся три места: метка носителя (claude-run.sh с подставным claude),
читатель лимит_из_потока (check-login-expiry.py) и _текущий (account-switch.py)
со счётчиком прямых вызовов. Двери мутаций: LOGIN_EXPIRY_UNDER_TEST,
ACCOUNT_SWITCH_UNDER_TEST, CLAUDE_RUN_UNDER_TEST — пути к копиям.

    python3 scripts/test_limit_from_stream.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGIN = Path(os.environ.get("LOGIN_EXPIRY_UNDER_TEST") or HERE / "check-login-expiry.py")
SWITCH = Path(os.environ.get("ACCOUNT_SWITCH_UNDER_TEST") or HERE / "account-switch.py")
RUN = Path(os.environ.get("CLAUDE_RUN_UNDER_TEST") or HERE / "claude-run.sh")
КЛЮЧ_А, КЛЮЧ_Б = "proba-klyuch-alpha", "proba-klyuch-beta"


def отпечаток(ключ):
    return hashlib.sha256(ключ.encode()).hexdigest()[:8]


def load(имя, путь):
    spec = importlib.util.spec_from_file_location(имя, путь)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def метка(чей):
    return json.dumps({"type": "harness_account", "отпечаток": чей})


def событие(статус, сброс, ут5, ут7, окно="five_hour"):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": статус, "resetsAt": сброс, "rateLimitType": окно,
        "unifiedWindows": {"five_hour": {"resetsAt": сброс, "utilization": ут5},
                           "seven_day": {"resetsAt": сброс + 1, "utilization": ут7}}}})


def разобрать(строка):
    try:
        return json.loads(строка)
    except ValueError:
        return None


def поток(каталог, имя, строки, возраст_с):
    путь = Path(каталог, имя)
    путь.write_text("\n".join(строки) + "\n", encoding="utf-8")
    t = time.time() - возраст_с
    os.utime(путь, (t, t))


def метка_носителя(каталог, доводы, ключ):
    """Первая строка stdout claude-run.sh с подставным claude → (строка, весь вывод)."""
    бин = Path(каталог, "claude")
    бин.write_text("#!/bin/sh\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n", encoding="utf-8")
    бин.chmod(0o755)
    сборщик = Path(каталог, "self-prompt.py")
    сборщик.write_text("import sys\nopen(sys.argv[sys.argv.index('--в') + 1], 'w')"
                       ".write('Метка сборки: проба\\n')\n", encoding="utf-8")
    for имя in ("harness.conf", "install.conf"):
        Path(каталог, имя).write_text("", encoding="utf-8")
    окружение = {к: з for к, з in os.environ.items() if к != "CLAUDE_CODE_OAUTH_TOKEN"}
    окружение.update(CLAUDE_BIN=str(бин), SELF_PROMPT_PY=str(сборщик), LOG_DIR=каталог,
                     HARNESS_CONF=str(Path(каталог, "harness.conf")),
                     HARNESS_INSTALL_CONF=str(Path(каталог, "install.conf")),
                     HARNESS_ACCOUNT_DIR=str(Path(каталог, "нет-аккаунтов")),
                     HARNESS_ALERT_TASK="/bin/true")
    if ключ:
        окружение["CLAUDE_CODE_OAUTH_TOKEN"] = ключ
    прогон = subprocess.run(["bash", str(RUN), "--класс", "демон", *доводы], env=окружение,
                            capture_output=True, text=True, timeout=60, cwd=каталог)
    вывод = прогон.stdout + прогон.stderr
    return (прогон.stdout.splitlines() or [""])[0], вывод


def main() -> int:
    for путь in (LOGIN, SWITCH, RUN):
        if not путь.is_file():
            print(f"ПРОБА СЛОМАНА: нет {путь}")
            return 1
    failed = total = 0

    def case(name, ok, got):
        nonlocal failed, total
        total += 1
        failed += not ok
        print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": получили {got!r}"))

    with tempfile.TemporaryDirectory(prefix="o-noapi-") as каталог:
        # ── метка носителя ──
        первая, вывод = метка_носителя(каталог, ["-p", "--output-format", "stream-json", "x"], КЛЮЧ_А)
        case("claude-run.sh: в начале потока stream-json — метка отпечатка ключа",
             разобрать(первая) == json.loads(метка(отпечаток(КЛЮЧ_А))), (первая, вывод[-300:]))
        case("claude-run.sh: ключа в выводе нет", КЛЮЧ_А not in вывод, вывод)
        первая, _ = метка_носителя(каталог, ["-p", "--output-format", "stream-json", "x"], "")
        case("claude-run.sh: без ключа в окружении — метка login",
             разобрать(первая) == json.loads(метка("login")), первая)
        первая, _ = метка_носителя(каталог, ["-p", "--output-format", "json", "x"], КЛЮЧ_А)
        case("claude-run.sh: не stream-json — вывод CLI без метки", "harness_account" not in первая, первая)

        # ── читатель ──
        потоки = Path(каталог, "demon-stream")
        потоки.mkdir()
        сброс = int(time.time()) + 3600
        поток(потоки, "a.jsonl", [метка(отпечаток(КЛЮЧ_А)), событие("allowed", сброс, 0.2, 0.4),
                                  метка(отпечаток(КЛЮЧ_Б)), событие("rejected", сброс, 1.0, 0.5),
                                  '{"type":"result"}'], 60)
        поток(потоки, "b.jsonl", [метка(отпечаток(КЛЮЧ_А)),
                                  событие("allowed_warning", сброс, 0.3, 0.9, окно="seven_day")], 300)
        поток(потоки, "c.jsonl", [метка("login"), событие("allowed", сброс, 0.1, 0.1)], 7200)
        m = load("cle_stream", LOGIN)
        m.подписочный_ключ = lambda *a: "proba-klyuch-login"
        д = m.лимит_из_потока(КЛЮЧ_Б, каталог=str(потоки))
        case("читатель: последний вызов файла — его метка и его событие",
             bool(д) and (д["статус"], д["сброс"], д["ут5"], д["ут7"], д["окно"])
             == ("rejected", сброс, 1.0, 0.5, "five_hour"), д)
        д = m.лимит_из_потока(КЛЮЧ_А, каталог=str(потоки))
        case("читатель: чужой последний вызов файла не приписан — взят свежий файл со своей меткой",
             bool(д) and (д["статус"], д["окно"], д["ут7"]) == ("allowed_warning", "seven_day", 0.9), д)
        д = m.лимит_из_потока("proba-klyuch-login", каталог=str(потоки))
        case("читатель: несвежий поток (2 ч) — None, а не старый статус", д is None, д)
        поток(потоки, "g.jsonl", [метка(отпечаток("proba-klyuch-zeta")),
                                  событие("rejected", сброс, 1.0, 1.0),
                                  метка(отпечаток("proba-klyuch-eta")), '{"type":"result"}'], 40)
        д = m.лимит_из_потока("proba-klyuch-eta", каталог=str(потоки))
        case("читатель: вызов без события лимита не берёт событие прежнего вызова", д is None, д)
        д = m.лимит_из_потока("proba-klyuch-gamma", каталог=str(потоки))
        case("читатель: метки нет нигде — None", д is None, д)
        поток(потоки, "d.jsonl", [метка("login"), событие("allowed", сброс, 0.15, 0.25)], 30)
        д = m.лимит_из_потока("proba-klyuch-login", каталог=str(потоки))
        case("читатель: ключ входа /login узнаёт метку login", bool(д) and д["ут5"] == 0.15, д)

        поток(потоки, "e.jsonl", [метка(отпечаток("proba-klyuch-delta")),
                                  событие("allowed", сброс, 0.5, 0.5)], 20)
        поток(потоки, "f.jsonl", [метка(отпечаток("proba-klyuch-eps")),
                                  событие("rejected", int(time.time()) - 60, 1.0, 0.5)], 20)

        # ── account-switch: _текущий ──
        вызовы = []
        m.доступ = lambda ключ, *a: вызовы.append(ключ) or {
            "орг": "", "повод": "", "код": 200, "статус": "allowed", "окно": "", "сброс": None,
            "ут5": None, "ут7": None, "повтор": None}
        m.ПОТОКИ = str(потоки)
        sw = load("switch_stream", SWITCH)
        sw._ключ_цели = lambda имя: {"beta": КЛЮЧ_Б, "gamma": "proba-klyuch-gamma",
                                     "delta": "proba-klyuch-delta", "eps": "proba-klyuch-eps"}.get(имя, "")
        имя, д, причина = sw._текущий("запуск", {"запуск": "beta"}, m)
        case("_текущий: лимит из потока, прямых вызовов 0",
             (имя, д.get("статус"), причина, len(вызовы)) == ("beta", "rejected", "", 0),
             (имя, д.get("статус"), причина, len(вызовы)))
        имя, д, причина = sw._текущий("запуск", {"запуск": "gamma"}, m)
        case("сосед: потока для ключа нет — прямой вызов, как прежде",
             (имя, len(вызовы)) == ("gamma", 1), (имя, д, причина, len(вызовы)))
        имя, д, причина = sw._текущий("запуск", {"запуск": "delta"}, m)
        case("«не в лимите» из потока могло устареть — его подтверждает живой вызов",
             (имя, len(вызовы)) == ("delta", 2), (имя, д, причина, len(вызовы)))
        имя, д, причина = sw._текущий("запуск", {"запуск": "eps"}, m)
        case("отказ из потока со сроком сброса в прошлом — не факт, живой вызов",
             (имя, д.get("статус"), len(вызовы)) == ("eps", "allowed", 3), (имя, д, причина, len(вызовы)))

    print(f"ИТОГ: {total - failed}/{total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
