#!/usr/bin/env python3
"""Проба: гейт секретов в логах берёт ИМЕНА ключей JSON секретом только там,
где имя ключа и есть секрет (сессии панели), а у учётки подписки — значения.

СЛУЧАЙ (05.11.2026). Гейт краснел на 7 копиях scripts/check-login-expiry.py
в каталоге логов: в них стоит имя поля учётки подписки (21 латинская буква,
вида «…ExpiresAt»). Имя поля верблюжьей записью проходит форму секрета, а
_строки_json кладёт в искомые каждое имя ключа любого JSON. То же имя стоит
в законном коде репозитория — гейт, краснеющий на коде, отключат.

КОНТРАКТ:
  * учётка подписки (JSON по пути из SECRET_SCAN_PATHS) отдаёт искомыми
    только ЗНАЧЕНИЯ: имя поля в логе — не находка;
  * значение поля той же учётки в логе — находка (чувствительность цела);
  * файл сессий панели (каталог секретов, ключ JSON — сама сессия): имя
    ключа в логе — находка;
  * гейт, запущенный процессом на чистом стенде, зелёный.

Значения порождаются при каждом прогоне и не печатаются; имена полей выдуманы
и длиннее 16 знаков, чтобы проходить форму, как настоящее. Механизм берётся
РЯДОМ с пробой: из копии дерева судится копия.

Запуск: python3 scripts/test_secret_in_logs_keys.py; 0 — зелёная, 1 — красная.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import secrets
import subprocess
import sys
import tempfile

ГЕЙТ = pathlib.Path(__file__).resolve().parent / "check-secret-in-logs.py"
# Имена полей учётки — по образцу настоящих («…ExpiresAt», «…ServerUrl»),
# но не они: проба не носит в себе строк живой учётки.
ПОЛЕ_СРОКА = "refreshTokenExpiresAtField"
ПОЛЕ_АДРЕСА = "authorizationServerUrlField"


def загрузить():
    spec = importlib.util.spec_from_file_location("secret_in_logs_gate", ГЕЙТ)
    модуль = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(модуль)
    return модуль


def значение(формы) -> str:
    """Сгенерированное значение, которое форма признаёт секретом: иначе случай
    «значение в логе» судил бы не гейт, а неудачный жребий."""
    while True:
        кандидат = secrets.token_urlsafe(32)
        if формы.это_секрет(кандидат):
            return кандидат


def стенд(tmp: str, формы, в_логе) -> dict:
    """Паспорт, конфиг, каталог секретов с сессиями панели, учётка подписки
    вне каталога и лог, в который пишет `в_логе(стенд)`."""
    корень = pathlib.Path(tmp)
    с = {"доступ": значение(формы), "обновление": значение(формы),
         "сессия": значение(формы), "корень": корень}
    каталог = корень / "secrets"
    каталог.mkdir()
    (каталог / "панель-сессии.json").write_text(
        json.dumps({с["сессия"]: {"ts": 1.0, "откуда": "127.0.0.1"}}), encoding="utf-8")
    учётка = корень / "home" / ".claude" / ".credentials.json"
    учётка.parent.mkdir(parents=True)
    учётка.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": с["доступ"], "refreshToken": с["обновление"],
                          "expiresAt": 1, ПОЛЕ_СРОКА: 2,
                          "scopes": ["user:inference"]},
        "mcpOAuth": {"plugin:stand|0000": {"discoveryState": {
            ПОЛЕ_АДРЕСА: "https://auth.example.invalid/"}}}}), encoding="utf-8")
    логи = корень / "logs"
    копия = логи / "disk-sweep-scratch" / "mut" / "1"
    копия.mkdir(parents=True)
    (копия / "check-login-expiry.py").write_text(в_логе(с), encoding="utf-8")
    репо = корень / "repo"
    репо.mkdir()
    с["паспорт"] = корень / "install.conf"
    с["паспорт"].write_text(f'SECRETS_DIR="{каталог}"\nLOG_DIR="{логи}"\n',
                            encoding="utf-8")
    с["конфиг"] = корень / "harness.conf"
    с["конфиг"].write_text(f'SECRET_SCAN_PATHS="{учётка}"\n', encoding="utf-8")
    с["логи"], с["репо"] = логи, репо
    return с


def код_с_именами(с) -> str:
    """Копия кода, читающего учётку: имена полей есть, значений нет."""
    return ('узел = данные.get("claudeAiOauth") or данные\n'
            f'срок = узел["{ПОЛЕ_СРОКА}"] / 1000\n'
            f'адрес = состояние.get("{ПОЛЕ_АДРЕСА}")\n')


def прогон(гейт, с) -> tuple[int, str]:
    поток = io.StringIO()
    with contextlib.redirect_stdout(поток):
        код = гейт.проверить(False, с["репо"], с["логи"], с["паспорт"], с["конфиг"])
    return код, поток.getvalue()


def main() -> int:
    гейт = загрузить()
    формы = гейт.формы
    упало = 0
    путей = 0

    def проба(имя: str, ждём: int, получили: int, вывод: str = "") -> None:
        nonlocal упало, путей
        путей += 1
        if ждём == получили:
            print(f"  ок    {имя}")
            return
        упало += 1
        первая = next((с for с in вывод.splitlines() if с.strip()), "")
        print(f"  ПЛОХО {имя}: ждали rc={ждём}, получили rc={получили}"
              + (f" — {первая}" if первая else ""))

    # Стенд воспроизводит случай, только если имя поля проходит форму, как
    # настоящее: иначе зелень случая 1 доказывала бы форму, а не гейт.
    проба("стенд: имена полей учётки проходят форму секрета", 0,
          0 if формы.это_секрет(ПОЛЕ_СРОКА) and формы.это_секрет(ПОЛЕ_АДРЕСА) else 1)

    with tempfile.TemporaryDirectory() as tmp:
        с = стенд(tmp, формы, код_с_именами)
        проба("БОЛЬНОЙ СЛУЧАЙ 05.11: имя поля учётки в копии кода — не находка", 0,
              *прогон(гейт, с))

    with tempfile.TemporaryDirectory() as tmp:
        с = стенд(tmp, формы, lambda с: код_с_именами(с)
                  + f'# + TOKEN={с["обновление"]}\n')
        проба("значение поля учётки в логе — находка", 1, *прогон(гейт, с))

    with tempfile.TemporaryDirectory() as tmp:
        с = стенд(tmp, формы, lambda с: код_с_именами(с)
                  + f'# сессия {с["сессия"]} обменяна\n')
        проба("ключ JSON сессий панели в логе — находка", 1, *прогон(гейт, с))

    # Живой запуск процессом: место паспорта, конфига и логов — окружением,
    # как у гейта на воротах. LOG_DIR задан и окружением: оно старше файла.
    with tempfile.TemporaryDirectory() as tmp:
        с = стенд(tmp, формы, lambda с: "чисто\n")
        окружение = {**os.environ, "HARNESS_INSTALL_CONF": str(с["паспорт"]),
                     "HARNESS_CONF": str(с["конфиг"]), "LOG_DIR": str(с["логи"])}
        итог = subprocess.run([sys.executable, str(ГЕЙТ)], env=окружение,
                              capture_output=True, text=True, timeout=300)
        проба("живой запуск гейта на чистом стенде — зелёный", 0,
              итог.returncode, итог.stdout + итог.stderr)
        проба("живой запуск судил стенд, а не боевой каталог логов", 0,
              0 if str(с["логи"]) in итог.stdout else 1, итог.stdout)

    print(f"{'ЗЕЛЁНАЯ' if not упало else 'КРАСНАЯ'}: путей {путей}, упало {упало}")
    return 1 if упало else 0


if __name__ == "__main__":
    sys.exit(main())
