#!/usr/bin/env python3
"""Проба: сторож входа по таймеру не шлёт прямой запрос к API модели.

Владелец 16.11.2026: прямой POST api.anthropic.com с ключом подписки мимо
Claude Code — убрать. Организацию входа /login сторож берёт из данных, которые
CLI сам пишет при входе (~/.claude.json, oauthAccount.organizationUuid);
организацию ключа профиля — из записи профиля (её сверяет account-switch.py
при заведении и переключении). Прямой запрос остаётся только за ключом
LOGIN_ORG_PROBE=1 в harness.conf.

Судится боевой main(): его соседи (tmux, файлы входа, вызов модели) заменены
подставными, а urllib.request.urlopen — счётчиком. Дверь мутаций:
LOGIN_EXPIRY_UNDER_TEST — путь к копии модуля.

    python3 scripts/test_login_organization_without_api.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SUBJECT = Path(os.environ.get("LOGIN_EXPIRY_UNDER_TEST")
               or Path(__file__).resolve().with_name("check-login-expiry.py"))
ВХОД = "11111111-1111-4111-8111-111111111111"
ПРОФИЛЯ = "22222222-2222-4222-8222-222222222222"
ЧУЖАЯ = "00000000-dead-4000-8000-000000000000"


def load():
    spec = importlib.util.spec_from_file_location("check_login_expiry_noapi", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Счётчик:
    """Подставной urlopen: считает обращения и отвечает «чужой» организацией."""

    def __init__(self):
        self.раз = 0

    def __call__(self, *_a, **_k):
        self.раз += 1
        raise urllib.error.HTTPError("http://127.0.0.1/x", 429, "лимит",
                                     {"anthropic-organization-id": ЧУЖАЯ}, None)


def прогон(каталог, проба_ключ, носитель, ключ_смены=""):
    """main() свежего модуля с подставными соседями → (вывод, обращений к сети)."""
    m = load()
    конф = Path(каталог, "harness.conf")
    строка_пробы = f'LOGIN_ORG_PROBE="{проба_ключ}"\n' if проба_ключ is not None else ""
    конф.write_text(f'LOGIN_EXPECTED_ORG="{ВХОД} {ПРОФИЛЯ}"\n' + строка_пробы, encoding="utf-8")
    cli = Path(каталог, "claude.json")
    cli.write_text(json.dumps({"oauthAccount": {"organizationUuid": ВХОД,
                                                "emailAddress": "x@example"}}), encoding="utf-8")
    счёт = Счётчик()
    профиль = {"имя": "alpha", "орг": ПРОФИЛЯ}
    откуда = {"вход": m.ОТКУДА_ПОДПИСКА, "профиль": "ключ профиля запуска alpha",
              "чужой": "переменная CLAUDE_CODE_OAUTH_TOKEN из профиля оболочки"}[носитель]
    ключ = {"вход": "ключ-входа", "профиль": "ключ-профиля", "чужой": "чужой"}[носитель]
    подмены = {
        "КОНФИГ_ДЕМОНОВ": str(конф), "ФАЙЛ_CLI": str(cli),
        "чужие_файлы": lambda: [], "сроки": lambda *a: None, "записать_историю": lambda *a: None,
        "ключ_в_обороте": lambda: (откуда, ключ), "записать_эталон": lambda *a, **k: False,
        "подписочный_ключ": lambda *a: "ключ-входа",
        "профиль_носителя": lambda к: профиль if к == "ключ-профиля" else None,
        "процесс_смены": lambda *a: "4242" if ключ_смены else None,
        "ключ_процесса": lambda pid: ключ_смены, "вход_после_старта": lambda *a: False,
        "оболочки_панелей": lambda *a: [], "отметиться": lambda *a: None,
    }
    for имя, значение in подмены.items():
        setattr(m, имя, значение)
    прежний_urlopen, прежние_доводы = urllib.request.urlopen, sys.argv
    urllib.request.urlopen, sys.argv = счёт, ["check-login-expiry.py", "--без-пробы"]
    вывод = io.StringIO()
    try:
        with contextlib.redirect_stdout(вывод):
            m.main()
    finally:
        urllib.request.urlopen, sys.argv = прежний_urlopen, прежние_доводы
    return вывод.getvalue(), счёт.раз


def строка(вывод, метка):
    return next((s for s in вывод.splitlines() if s.startswith(метка)), "")


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

    with tempfile.TemporaryDirectory(prefix="o-noapi-") as каталог:
        вывод, раз = прогон(каталог, None, "вход")
        акк = строка(вывод, "[аккаунт]")
        case("БОЛЬНОЙ: ключа LOGIN_ORG_PROBE нет — к сети 0 обращений", раз == 0, раз)
        case("вход /login: организация из oauthAccount ~/.claude.json",
             ВХОД in акк and "аккаунт тот" in акк, акк)
        case("прямой запрос выключен — сказано словами", "не сверяется прямым запросом" in акк, акк)

        вывод, раз = прогон(каталог, "0", "профиль", ключ_смены="ключ-профиля")
        case("LOGIN_ORG_PROBE=0, ключ профиля у носителя и у смены — 0 обращений", раз == 0, раз)
        case("ключ профиля: организация из записи профиля",
             "аккаунт тот" in строка(вывод, "[аккаунт]") and ПРОФИЛЯ in строка(вывод, "[аккаунт]")
             and строка(вывод, "[смена]").startswith(f"[смена] на профиле alpha ({ПРОФИЛЯ})"), вывод)

        вывод, раз = прогон(каталог, "0", "вход", ключ_смены="ключ-профиля")
        case("носитель /login, смена на ключе профиля — свой разбор смены, 0 обращений",
             раз == 0 and строка(вывод, "[смена]").startswith(f"[смена] на профиле alpha ({ПРОФИЛЯ})"),
             (раз, строка(вывод, "[смена]")))

        вывод, раз = прогон(каталог, "0", "чужой")
        акк = строка(вывод, "[аккаунт]")
        case("чужой носитель: 0 обращений", раз == 0, раз)
        case("чужой носитель: не «аккаунт тот», а честное «не сверяется»",
             "аккаунт тот" not in акк and "не сверяется прямым запросом" in акк, акк)

        вывод, раз = прогон(каталог, "1", "вход")
        case("сосед: LOGIN_ORG_PROBE=1 — прямой запрос, как прежде", раз == 1, раз)
        case("сосед: при 1 организация — из ответа", ЧУЖАЯ in строка(вывод, "[аккаунт]"), вывод)

    print(f"ИТОГ: {total - failed}/{total}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
