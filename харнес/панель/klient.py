#!/usr/bin/env python3
"""Тонкий клиент панели для диспетчера: спросить службу, а не лезть в её файлы.

Почему так. Состояние панели (токены входа, сессии, ожидания подтверждений)
пишет ОДНА сторона — служба, под своим пользователем. Когда в те же файлы писал
ещё и диспетчер под своим, права молча расходились: файл создавался группой
пишущего, и вторая сторона его не читала. Живая улика 11.09.2026 — владелец
открыл присланную ссылку и увидел «устарела», хотя токен был свежий.

Поэтому диспетчер ничего не пишет, а зовёт службу по localhost. Эти маршруты
доступны только с самой машины (служба слушает 127.0.0.1 и отличает прямой
вызов от прошедшего через nginx), а снаружи закрыты в конфиге nginx.

Прогон:
    klient.py --ссылка <chat_id>      → ссылка входа или причина отказа
    klient.py --выйти <chat_id>       → отозвать все пропуска
    klient.py --ответ "да 7391"       → отдать службе ответ владельца
"""
import json
import os
import sys
import urllib.error
import urllib.request

АДРЕС = os.environ.get("PANEL_LOCAL", "http://127.0.0.1:8787")


def внутренний_ключ() -> str:
    """Общий секрет со службой: маршруты /vnutr/ доказываются им (F-sec-01)."""
    каталог = (os.environ.get("PANEL_SECRETS_DIR")
               or os.environ.get("SECRETS_DIR") or "/var/lib/harness/panel-state")
    try:
        with open(os.path.join(каталог, "панель-внутренний-ключ"),
                  encoding="utf-8") as файл:
            return файл.read().strip()
    except OSError:
        return ""


def позвать(путь: str, данные: dict) -> tuple[int, dict]:
    запрос = urllib.request.Request(
        f"{АДРЕС}{путь}", data=json.dumps(данные).encode(),
        headers={"Content-Type": "application/json",
                 "X-Panel-Key": внутренний_ключ()}, method="POST")
    try:
        with urllib.request.urlopen(запрос, timeout=10) as ответ:
            return ответ.status, json.loads(ответ.read().decode("utf-8"))
    except urllib.error.HTTPError as беда:
        try:
            return беда.code, json.loads(беда.read().decode("utf-8"))
        except ValueError:
            return беда.code, {}
    except OSError as беда:
        return 0, {"беда": f"служба панели не отвечает: {беда}"}


def main(argv: list[str]) -> int:
    if "--ссылка" in argv:
        код, ответ = позвать("/vnutr/token", {"chat_id": argv[argv.index("--ссылка") + 1]})
        print(ответ.get("ссылка") or ответ.get("беда") or "панель не ответила")
        return 0 if код == 200 else 2
    if "--выйти" in argv:
        код, ответ = позвать("/vnutr/vyjti", {"chat_id": argv[argv.index("--выйти") + 1]})
        print("все пропуска панели отозваны" if код == 200
              else ответ.get("беда", "не вышло"))
        return 0 if код == 200 else 2
    if "--ответ" in argv:
        код, ответ = позвать("/vnutr/otvet", {"текст": argv[argv.index("--ответ") + 1]})
        # Служба недоступна — это НЕ «ответ панели»: сообщение владельца должно
        # пойти своим обычным путём, а не пропасть.
        print(ответ.get("итог", "не ответ панели") if код == 200 else "не ответ панели")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
