#!/usr/bin/env python3
"""Проба: код 79 носителя — не «ДОСТУП НЕ РАБОТАЕТ».

Демоны зовут модель через обёртку демонов → claude-run.sh (hr.standards-and-policy
§4.4). Носитель отдаёт 79, когда личность не собрана, и сам шлёт об этом сигнал.
Боевая проба входа (check-login-expiry.py) разбирает код того же пути: до правки
79 падал в общее «ДОСТУП НЕ РАБОТАЕТ», и владелец получал ложную тревогу о входе
поверх сигнала о сборке, а чинить вход было нечего.

Модуль судится импортом (дефис в имени — importlib), чистой функцией
вердикт_доступа. Дверь мутаций: LOGIN_EXPIRY_UNDER_TEST — путь к копии модуля.

    python3 scripts/test_login_expiry_exit79.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import importlib.util
import os
import sys
from pathlib import Path

SUBJECT = Path(os.environ.get("LOGIN_EXPIRY_UNDER_TEST")
               or Path(__file__).resolve().with_name("check-login-expiry.py"))


def load():
    spec = importlib.util.spec_from_file_location("check_login_expiry", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if not SUBJECT.is_file():
        print(f"ПРОБА СЛОМАНА: нет {SUBJECT}")
        return 1
    verdict = load().вердикт_доступа
    failed = 0
    total = 0

    def case(name, ok, got):
        nonlocal failed, total
        total += 1
        failed += not ok
        print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": получили {got!r}"))

    soft = "вход не проверен: личность не собрана"
    got = verdict(79, "x")
    case("БОЛЬНОЙ: 79 → (0, «вход не проверен: личность не собрана…»)",
         got[0] == 0 and got[1].startswith(soft), got)
    case("79: слов «ДОСТУП НЕ РАБОТАЕТ» нет", "ДОСТУП НЕ РАБОТАЕТ" not in got[1], got)
    # Тот же вердикт без входа и с выводом «Not logged in»: 79 значит, что claude
    # не запускался, и его вывод о входе здесь взяться не может.
    got = verdict(79, "", вход_есть=False)
    case("79 без файла входа — тот же мягкий вердикт", got[0] == 0 and got[1].startswith(soft), got)
    # Соседи: ветка 79 не съела прежние вердикты.
    got = verdict(0, "ответ")
    case("сосед: 0 — доступ живой", got[0] == 0 and "ЖИВОЙ" in got[1], got)
    got = verdict(77, "лимит")
    case("сосед: 77 — лимит, мягко", got[0] == 0 and "лимит" in got[1], got)
    got = verdict(1, "Your organization has disabled Claude subscription access")
    case("сосед: 1 — ДОСТУП НЕ РАБОТАЕТ, как прежде",
         got[0] == 1 and "ДОСТУП НЕ РАБОТАЕТ" in got[1], got)
    got = verdict(78, "")
    case("сосед: 78 (пауза) не стал мягким вердиктом личности", soft not in got[1], got)

    print(f"\nитог test_login_expiry_exit79: случаев {total}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
