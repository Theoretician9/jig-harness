#!/usr/bin/env python3
"""Проба: файл входа, переписанный посреди смены ТЕМ ЖЕ аккаунтом, — не красное.

Находка копии сети 20.11.2026: правило «вход после старта»
(check-login-expiry.py) краснело у любой долгой смены — демон обновления доступа
переписывает файл входа посреди смены (смена живёт десятки часов), и обновление
откатывалось самопроверкой. Правило защищает от другого: «/login» внутри сессии
сменил аккаунт на лету (улика 13.11.2026: ключ окружения nikita-f3d, работа шла
на копии сети). Аккаунт входа /login — организация из ~/.claude.json
(oauthAccount.organizationUuid); аккаунт смены — организация её ключа. Красным
остаётся только запись файла входа ДРУГИМ аккаунтом.

Судится боевой main() с настоящим `вход_после_старта`: файл входа стенда
переписан после старта этого процесса (он и есть «процесс смены»). Соседи (tmux,
профили, вызов модели) — подставные. Дверь мутаций: LOGIN_EXPIRY_UNDER_TEST —
путь к копии модуля.

    python3 scripts/test_login_after_start_same_account.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SUBJECT = Path(os.environ.get("LOGIN_EXPIRY_UNDER_TEST")
               or Path(__file__).resolve().with_name("check-login-expiry.py"))
ПРОФИЛЯ = "22222222-2222-4222-8222-222222222222"
ЧУЖАЯ = "33333333-3333-4333-8333-333333333333"


def load():
    spec = importlib.util.spec_from_file_location("check_login_expiry_same_acc", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def прогон(каталог, орг_входа, переписан_после_старта=True):
    """main() свежего модуля → строка «[смена]» его вывода."""
    m = load()
    конф = Path(каталог, "harness.conf")
    конф.write_text(f'LOGIN_EXPECTED_ORG="{ПРОФИЛЯ}"\nLOGIN_ORG_PROBE="0"\n', encoding="utf-8")
    cli = Path(каталог, "claude.json")
    cli.write_text(json.dumps({"oauthAccount": {"organizationUuid": орг_входа}}), encoding="utf-8")
    вход = Path(каталог, "credentials.json")
    вход.write_text('{"claudeAiOauth": {}}', encoding="utf-8")
    старт = m.старт_процесса(os.getpid())
    if старт is None:
        raise SystemExit("ПРОБА СЛОМАНА: старт своего процесса не прочесть")
    # После старта — как запись демона посреди смены; до — прежний вход.
    момент = time.time() if переписан_после_старта else старт - 3600
    os.utime(вход, (момент, момент))
    профиль = {"имя": "alpha", "орг": ПРОФИЛЯ}
    подмены = {
        "КОНФИГ_ДЕМОНОВ": str(конф), "ФАЙЛ_CLI": str(cli), "ФАЙЛ": str(вход),
        "чужие_файлы": lambda: [], "сроки": lambda *a: None, "записать_историю": lambda *a: None,
        "ключ_в_обороте": lambda: ("ключ профиля запуска alpha", "ключ-профиля"),
        "записать_эталон": lambda *a, **k: False,
        "подписочный_ключ": lambda *a: "ключ-входа",
        "профиль_носителя": lambda к: профиль if к == "ключ-профиля" else None,
        "процесс_смены": lambda *a: str(os.getpid()),
        "ключ_процесса": lambda pid: "ключ-профиля",
        "оболочки_панелей": lambda *a: [], "отметиться": lambda *a: None,
    }
    for имя, значение in подмены.items():
        if not hasattr(m, имя):
            raise SystemExit(f"ПРОБА СЛОМАНА: в модуле нет {имя} — подмена не судит ничего")
        setattr(m, имя, значение)
    прежние = sys.argv
    sys.argv = ["check-login-expiry.py", "--без-пробы"]
    вывод = io.StringIO()
    try:
        with contextlib.redirect_stdout(вывод):
            m.main()
    finally:
        sys.argv = прежние
    return next((s for s in вывод.getvalue().splitlines() if s.startswith("[смена]")), ""), m


def main() -> int:
    if not SUBJECT.is_file():
        print(f"ПРОБА СЛОМАНА: нет {SUBJECT}")
        return 1
    failed = total = 0

    def case(name, ok, got):
        nonlocal failed, total
        total += 1
        failed += not ok
        print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": получили {got!r}"))

    with tempfile.TemporaryDirectory(prefix="o-login-same-") as каталог:
        смена, m = прогон(каталог, ПРОФИЛЯ)
        case("БОЛЬНОЙ СЛУЧАЙ копии сети: файл входа переписан после старта ТЕМ ЖЕ аккаунтом — не красное",
             m.ВХОД_В_СЕССИИ not in смена and смена.startswith(f"[смена] на профиле alpha ({ПРОФИЛЯ})"),
             смена)

        смена, m = прогон(каталог, ЧУЖАЯ)
        case("улика 13.11: переписан после старта ДРУГИМ аккаунтом — красное «вход в сессии»",
             m.ВХОД_В_СЕССИИ in смена, смена)

        смена, m = прогон(каталог, "")
        case("аккаунт входа не прочесть — красное остаётся (не доказано, что тот же)",
             m.ВХОД_В_СЕССИИ in смена, смена)

        смена, m = прогон(каталог, ЧУЖАЯ, переписан_после_старта=False)
        case("сосед: файл входа старше смены — правило молчит, судит разбор смены",
             m.ВХОД_В_СЕССИИ not in смена and смена.startswith("[смена] на профиле alpha"), смена)

    print(f"ИТОГ: {total - failed}/{total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
