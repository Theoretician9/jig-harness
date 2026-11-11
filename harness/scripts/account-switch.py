#!/usr/bin/env python3
"""Дверь записи профилей и запуска смены на аккаунте по ключу setup-token.

Единственное место, которое пишет profiles.json и state.json (под замком
`.lock`, tmp+rename). Каждая запись идёт после ЖИВОГО вызова ключом: смена,
поднятая на мёртвом ключе, не сможет даже откатить себя сама.

    --профиль-добавить <имя> --подпись S --орг O --ключ <файл в SECRETS_DIR> [--почта E]
    --запуск <имя|login>     профиль, на котором поднимется СЛЕДУЮЩИЙ процесс смены
    --проверить              процесс смены несёт ключ профиля `запуск` и орг сошлась

Печатает имя, отпечаток, организацию — ни ключа, ни пути файла ключа.
Коды: 0 — сделано, 1 — отказ или расхождение, 2 — проверить нечем.
"""
import argparse
import datetime
import hashlib
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
import accounts  # noqa: E402


def _сторож():
    """check-login-expiry.py — тот же код, не копия: процесс смены, ключ процесса, организация."""
    spec = importlib.util.spec_from_file_location("check_login_expiry", os.path.join(HERE, "check-login-expiry.py"))
    модуль = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(модуль)
    return модуль


def отказ(текст: str) -> int:
    print(f"ОТКАЗ: {текст}", file=sys.stderr)
    return 1


def _отпечаток(ключ: str) -> str:
    return hashlib.sha256(ключ.encode()).hexdigest()[:8]


def _сейчас() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _каталог_есть():
    if os.path.isdir(accounts.каталог()):
        return None
    return отказ(f"каталога аккаунтов нет; создать: sudo install -d -o agent -g agent -m 750 {accounts.каталог()}")


def _живой_ключ(имя_файла: str, сторож):
    """(ключ, орг, "") или ("", "", причина): файл годен и живой вызов принят."""
    путь, причина = accounts.путь_ключа(имя_файла)
    if причина:
        return "", "", причина
    ключ = accounts.ключ(путь)
    орг, повод = сторож.организация(ключ)
    return (ключ, орг, "") if орг else ("", "", f"ключ не принят: {повод}")


def добавить(арг) -> int:
    if not accounts.ИМЯ.fullmatch(арг.профиль_добавить):
        return отказ("имя профиля — [a-z0-9-], 1-20 знаков")
    if (r := _каталог_есть()) is not None:
        return r
    if accounts.профиль(арг.профиль_добавить):
        return отказ(f"профиль {арг.профиль_добавить} уже заведён: имя неизменно")
    if not арг.орг or not арг.подпись:
        return отказ("нужны --подпись и --орг")
    сторож = _сторож()
    ключ, орг, причина = _живой_ключ(арг.ключ, сторож)
    if причина:
        return отказ(причина)
    ожидаемые = сторож.ожидаемые_орг()
    if орг != арг.орг:
        return отказ(f"живой вызов назвал организацию {орг}, а не {арг.орг}")
    if орг not in ожидаемые:
        return отказ(f"организация {орг} не в LOGIN_EXPECTED_ORG")
    запись = {"имя": арг.профиль_добавить, "подпись": арг.подпись, "орг": орг, "почта": арг.почта or "",
              "ключ": арг.ключ, "отпечаток": _отпечаток(ключ)}

    def вставить(данные):
        if any(p.get("имя") == запись["имя"] for p in данные.setdefault("профили", [])):
            raise ValueError("профиль уже заведён")
        данные["профили"].append(запись)
    accounts.записать_профили(вставить)
    accounts.записать_состояние(lambda s: s.setdefault("запуск", "login"))
    print(f"профиль {запись['имя']} заведён: отпечаток {запись['отпечаток']}, организация {орг}")
    return 0


def запуск(арг) -> int:
    цель = арг.запуск
    if цель != "login" and not accounts.ИМЯ.fullmatch(цель):
        return отказ("цель — login или имя [a-z0-9-]")
    if (r := _каталог_есть()) is not None:
        return r
    if цель != "login":
        проф = accounts.профиль(цель)
        if проф is None:
            return отказ(f"профиль {цель} не заведён")
        _, орг, причина = _живой_ключ(проф.get("ключ"), _сторож())
        if причина:
            return отказ(f"профиль {цель}: {причина}")
        if орг != проф.get("орг"):
            return отказ(f"профиль {цель}: организация ключа {орг} не та, что у профиля")

    def записать(s):
        откуда = s.get("запуск", "")
        s["запуск"] = цель
        s.setdefault("переключения", []).append(
            {"время": _сейчас(), "откуда": откуда, "куда": цель, "что": "запуск"})
    try:
        accounts.записать_состояние(записать)
    except ValueError:
        return отказ("state.json битый — не затираю")
    print(f"запуск следующей смены: {цель}")
    return 0


def проверить(_арг) -> int:
    сторож = _сторож()
    pid = сторож.процесс_смены()
    if pid is None:
        print("процесса смены в панели нет — проверять нечего", file=sys.stderr)
        return 2
    стат = accounts.состояние()
    if стат is None:
        print("state.json не читается", file=sys.stderr)
        return 2
    цель = стат.get("запуск")
    ключ = сторож.ключ_процесса(pid)
    if ключ is None:
        print("окружение процесса смены не прочесть", file=sys.stderr)
        return 2
    if цель == "login":
        if ключ:
            return отказ("запуск=login, а процесс смены несёт ключ")
        итог = "login: ключа в процессе нет"
    else:
        проф = accounts.профиль(цель) if isinstance(цель, str) else None
        путь, причина = accounts.файл_ключа(проф) if проф else ("", "профиль запуска не заведён")
        if причина:
            return отказ(причина)
        if not ключ or ключ != accounts.ключ(путь):
            return отказ(f"процесс смены несёт не ключ профиля {цель} (смена не переподнята?)")
        орг, повод = сторож.организация(ключ)
        if not орг:
            print(f"организацию узнать не удалось: {повод}", file=sys.stderr)
            return 2
        if орг != проф.get("орг"):
            return отказ(f"организация ключа процесса {орг} не та, что у профиля {цель}")
        итог = f"{цель}: отпечаток {_отпечаток(ключ)}, организация {орг}"
    accounts.записать_состояние(lambda s: s.__setitem__("идёт", цель))
    print(f"смена идёт на {итог}")
    return 0


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    р.add_argument("--профиль-добавить", dest="профиль_добавить")
    р.add_argument("--подпись")
    р.add_argument("--орг")
    р.add_argument("--ключ")
    р.add_argument("--почта")
    р.add_argument("--запуск")
    р.add_argument("--проверить", action="store_true")
    арг = р.parse_args()
    if арг.профиль_добавить:
        return добавить(арг)
    if арг.запуск:
        return запуск(арг)
    if арг.проверить:
        return проверить(арг)
    р.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
