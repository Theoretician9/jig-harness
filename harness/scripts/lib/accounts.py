"""Профили аккаунтов смены: каталог, чтение, файл ключа, запись под замком.

Каталог `$STATE_DIR/account/` (или HARNESS_ACCOUNT_DIR): profiles.json и
state.json — JSON, пишет только дверь scripts/account-switch.py. Ключ здесь
никогда не печатается: наружу идут имя, отпечаток и ПУТЬ файла ключа — путь
получает только обёртка as-account.sh и только в переменную, не в вывод.

CLI для обёртки:
    python3 accounts.py --файл-ключа-запуска
печатает одну строку: `login` | абсолютный путь файла ключа | `ОТКАЗ <причина>`
(код 3). Нет profiles.json — возможность не заведена, это `login` без вести.
"""
import contextlib
import fcntl
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import конфиг  # noqa: E402

ИМЯ = re.compile(r"[a-z0-9-]{1,20}")
ЛИМИТ_ПЕРЕКЛЮЧЕНИЙ = 20


def каталог() -> str:
    return os.environ.get("HARNESS_ACCOUNT_DIR") or os.path.join(конфиг()["STATE_DIR"], "account")


def _прочесть(имя_файла: str):
    """Содержимое JSON-файла каталога: None — файла нет, ValueError — битый."""
    try:
        with open(os.path.join(каталог(), имя_файла), encoding="utf-8") as fh:
            данные = json.load(fh)
    except FileNotFoundError:
        return None
    if not isinstance(данные, dict):
        raise ValueError(f"{имя_файла}: не объект")
    return данные


def заведено() -> bool:
    return os.path.exists(os.path.join(каталог(), "profiles.json"))


def профили() -> list:
    """Профили как список словарей; нет файла или он битый — пусто."""
    try:
        данные = _прочесть("profiles.json") or {}
    except (ValueError, OSError):
        return []
    return [p for p in данные.get("профили", []) if isinstance(p, dict)]


def профиль(имя: str):
    return next((p for p in профили() if p.get("имя") == имя), None)


def состояние():
    """state.json как словарь; None — файла нет или он не читается."""
    try:
        return _прочесть("state.json")
    except (ValueError, OSError):
        return None


def путь_ключа(имя_файла) -> tuple:
    """(абсолютный путь, "") или ("", причина). Имя без «/», файл внутри SECRETS_DIR, непустой."""
    if not isinstance(имя_файла, str) or not имя_файла or "/" in имя_файла or имя_файла in (".", ".."):
        return "", "имя файла ключа некорректно"
    корень = os.path.realpath(конфиг()["SECRETS_DIR"])
    путь = os.path.realpath(os.path.join(корень, имя_файла))
    if os.path.dirname(путь) != корень:
        return "", "файл ключа вне каталога секретов"
    if not os.path.isfile(путь):
        return "", "файла ключа нет"
    try:
        пусто = not open(путь, encoding="utf-8").read().strip()
    except (OSError, UnicodeDecodeError):
        return "", "файл ключа не прочесть"
    return ("", "файл ключа пуст") if пусто else (путь, "")


def файл_ключа(проф: dict) -> tuple:
    return путь_ключа((проф or {}).get("ключ"))


def ключ(путь: str) -> str:
    with open(путь, encoding="utf-8") as fh:
        return fh.read().strip()


@contextlib.contextmanager
def замок():
    каталог_ = каталог()
    if not os.path.isdir(каталог_):
        raise FileNotFoundError(каталог_)
    with open(os.path.join(каталог_, ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _записать_атомарно(имя_файла: str, данные: dict):
    путь = os.path.join(каталог(), имя_файла)
    tmp = f"{путь}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(данные, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, путь)


def записать_профили(изменить):
    """Под замком: прочесть profiles.json → изменить(dict) → записать."""
    with замок():
        данные = _прочесть("profiles.json") or {"профили": []}
        изменить(данные)
        _записать_атомарно("profiles.json", данные)


def записать_состояние(изменить):
    """Под замком: прочесть state.json (нет — пустое) → изменить(dict) → записать.

    Битый файл не затирается молча: исключение уходит вызывающему.
    """
    with замок():
        данные = _прочесть("state.json") or {}
        изменить(данные)
        данные["переключения"] = данные.get("переключения", [])[-ЛИМИТ_ПЕРЕКЛЮЧЕНИЙ:]
        _записать_атомарно("state.json", данные)


def файл_запуска() -> tuple:
    """(строка для обёртки, код): login | путь | ОТКАЗ причина."""
    if not заведено():
        return "login", 0
    стат = состояние()
    if стат is None:
        return "ОТКАЗ state.json нет или не читается", 3
    имя = стат.get("запуск")
    if имя == "login":
        return "login", 0
    проф = профиль(имя) if isinstance(имя, str) else None
    if проф is None:
        return f"ОТКАЗ профиль запуска «{имя}» не заведён", 3
    путь, причина = файл_ключа(проф)
    if причина:
        return f"ОТКАЗ профиль {имя} (отпечаток {проф.get('отпечаток', '')}): {причина}", 3
    return путь, 0


def main() -> int:
    if "--файл-ключа-запуска" in sys.argv:
        try:
            строка, код = файл_запуска()
        except Exception as беда:  # обёртка не должна падать: любой сбой — отказ с причиной
            строка, код = f"ОТКАЗ разбор не удался: {type(беда).__name__}", 3
        print(строка)
        return код
    print("использование: accounts.py --файл-ключа-запуска", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
