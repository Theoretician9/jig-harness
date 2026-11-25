#!/usr/bin/env python3
"""Проверка входа `web_app`: подпись Ed25519 данных запуска Telegram.

Спека: спека живой карты (.tasks/<задача>/спека.md) §7 (дверь из Telegram),
К-6 (часть «Проба сервера: `Authorization: tma` …»); план — Task 4, условие
ревью Н-1. Контракт модуля:

    tg_web_app.verify(init_data, *, bot_id, pubkey_hex, owner_id, max_age_s, now) -> bool

Стенд подписывает своим ключом `Ed25519PrivateKey.generate()` и подаёт его
открытый ключ как `pubkey_hex`: боевой ключ Telegram и токен бота проба не
трогает. Строка подписи — `<bot_id>:WebAppData\\n` и пары без `hash` и
`signature` по алфавиту через `\\n`; подпись — base64url без `=`.

Время пробы — довод `now`, далёкий от часов машины: модуль, который смотрит
в `time.time()` вместо довода, краснеет на путях возраста.

Дверь пробы: `PANEL_SERVER_DIR` — каталог, откуда берётся `tg_web_app.py`
(по умолчанию — каталог этой пробы). Мутации судятся на копии каталога.

Прогон:
    python3 harness/panel/test_tg_web_app.py
"""
import base64
import json
import os
import sys
import urllib.parse
from pathlib import Path

MODULE_DIR = Path(os.environ.get("PANEL_SERVER_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(MODULE_DIR))

путей = 0
неудач = 0


def путь_ок(текст):
    global путей
    путей += 1
    print("ок  · " + текст)


def путь_плохо(текст):
    global путей, неудач
    путей += 1
    неудач += 1
    print("ПЛОХО · " + текст)


def итог_и_выход():
    print(f"ВХОД-WEB_APP: путей {путей}, неудач {неудач}")
    sys.exit(1 if неудач else 0)


try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError as беда:
    путь_плохо(f"нет cryptography у интерпретатора пробы (Н-9): {беда}")
    итог_и_выход()

try:
    import tg_web_app
except Exception as беда:   # модуля нет — это ответ пробы, а не трассировка
    путь_плохо(f"модуля tg_web_app нет в {MODULE_DIR}: {беда}")
    итог_и_выход()

BOT_ID = "777000111"
OWNER_ID = "100000001"
MAX_AGE_S = 43200                 # LIVE_MAP_TG_AUTH_S по умолчанию — 12 ч
NOW = 1_700_000_000.0             # нарочно не часы машины
KEY = Ed25519PrivateKey.generate()
PUBKEY_HEX = KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
OTHER_KEY = Ed25519PrivateKey.generate()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def init_data(*, user_id=OWNER_ID, auth_date=None, bot_id=BOT_ID, key=KEY,
              with_hash=True, extra=None, after_sign=None) -> str:
    """Данные запуска так, как их отдаёт Telegram третьей стороне: пары
    urlencode, `signature` — Ed25519 над строкой проверки, `hash` — HMAC,
    которого панель проверить не может и не обязана."""
    pairs = {
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        # Кириллица и пробел в имени: строка проверки строится по
        # РАЗОБРАННЫМ значениям, а не по сырому urlencode.
        "user": json.dumps({"id": int(user_id), "first_name": "Владелец Проба",
                            "username": "vladelec"}, ensure_ascii=False,
                           separators=(",", ":")),
        "auth_date": str(int(NOW - 60) if auth_date is None else int(auth_date)),
    }
    pairs.update(extra or {})
    check = f"{bot_id}:WebAppData\n" + "\n".join(
        f"{k}={v}" for k, v in sorted(pairs.items()))
    signed = dict(pairs, signature=b64url(key.sign(check.encode("utf-8"))))
    if with_hash:
        signed["hash"] = "c501b71e775f74ce10e377dea85a7ea24ecd640b223ea86dfe453e0eaed2e2b2"
    signed.update(after_sign or {})
    return urllib.parse.urlencode(signed)


def verify(data, **меняем):
    """Вызов модуля; исключение — тоже неудача: контракт велит «любая ошибка
    разбора — False», а служба на исключении закрыла бы соединение."""
    доводы = dict(bot_id=BOT_ID, pubkey_hex=PUBKEY_HEX, owner_id=OWNER_ID,
                  max_age_s=MAX_AGE_S, now=NOW)
    доводы.update(меняем)
    try:
        return tg_web_app.verify(data, **доводы)
    except Exception as беда:
        return f"исключение {type(беда).__name__}: {беда}"


def ждём(имя, ждали, вышло):
    if вышло is ждали:
        путь_ок(имя)
    else:
        путь_плохо(f"{имя}: ждали {ждали}, вышло {вышло!r}")


def испорченная_подпись(data: str) -> str:
    """Тот же набор, у подписи изменён один байт (второй: первый знак base64
    кодирует лишь его старшие биты)."""
    pairs = dict(urllib.parse.parse_qsl(data, keep_blank_values=True))
    raw = bytearray(base64.urlsafe_b64decode(pairs["signature"] + "=="))
    raw[1] ^= 0x01
    pairs["signature"] = b64url(bytes(raw))
    return urllib.parse.urlencode(pairs)


# ── здоровый путь ───────────────────────────────────────────────────────────
ждём("верная подпись стенда — True", True, verify(init_data()))
ждём("верная подпись без поля hash — True", True, verify(init_data(with_hash=False)))
# `hash` — HMAC Telegram; панели нечем его проверить. Если модуль не убирает
# его из строки проверки, верная подпись перестаёт сходиться.
ждём("hash в данных не мешает (не входит в строку проверки)", True,
     verify(init_data(with_hash=True)))
ждём("лишние поля Telegram (chat_type, chat_instance) входят в строку и не ломают её",
     True, verify(init_data(extra={"chat_type": "private", "chat_instance": "-42"})))
ждём("возраст ровно max_age_s — ещё True (граница «≤»)", True,
     verify(init_data(auth_date=NOW - MAX_AGE_S)))
ждём("возраст считается от довода now, а не от часов машины — True", True,
     verify(init_data(auth_date=NOW - 120)))

# ── больные пути: подпись ──────────────────────────────────────────────────
ждём("БОЛЬНОЙ: испорчен один байт подписи — False", False,
     verify(испорченная_подпись(init_data())))
ждём("БОЛЬНОЙ: подписано чужим ключом — False", False, verify(init_data(key=OTHER_KEY)))
ждём("БОЛЬНОЙ: подпись для другого бота — False (bot_id входит в строку)", False,
     verify(init_data(bot_id="999000222")))
ждём("БОЛЬНОЙ: поле изменено после подписи (auth_date свежее) — False", False,
     verify(init_data(after_sign={"auth_date": str(int(NOW - 1))})))
ждём("БОЛЬНОЙ: подписи нет совсем — False", False,
     verify(init_data(after_sign={"signature": ""})))
ждём("БОЛЬНОЙ: подпись не base64 — False, без исключения", False,
     verify(init_data(after_sign={"signature": "!!!не-base64!!!"})))

# ── больные пути: кто и когда ──────────────────────────────────────────────
ждём("БОЛЬНОЙ: чужой user.id при верной подписи — False", False,
     verify(init_data(user_id="100000002")))
ждём("БОЛЬНОЙ: auth_date старше 12 ч при верной подписи — False", False,
     verify(init_data(auth_date=NOW - MAX_AGE_S - 60)))
ждём("БОЛЬНОЙ: max_age_s берётся из довода (3600, возраст 7200) — False", False,
     verify(init_data(auth_date=NOW - 7200), max_age_s=3600))

# ── больные пути: ключи и разбор ───────────────────────────────────────────
ждём("БОЛЬНОЙ: пустой bot_id — False", False, verify(init_data(), bot_id=""))
ждём("БОЛЬНОЙ: пустой pubkey_hex — False", False, verify(init_data(), pubkey_hex=""))
ждём("БОЛЬНОЙ: pubkey_hex не hex — False, без исключения", False,
     verify(init_data(), pubkey_hex="zz" * 32))
ждём("БОЛЬНОЙ: пустые данные — False", False, verify(""))
ждём("БОЛЬНОЙ: мусор вместо данных — False, без исключения", False,
     verify("%%%&&&==;;"))
ждём("БОЛЬНОЙ: user не JSON — False, без исключения", False,
     verify(init_data(after_sign={"user": "{не json"})))
ждём("БОЛЬНОЙ: auth_date не число — False, без исключения", False,
     verify(init_data(after_sign={"auth_date": "вчера"})))
ждём("БОЛЬНОЙ: нет поля user — False", False,
     verify(urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(init_data())
                                    if k != "user"])))

# Н-1: у панели нет и не будет токена бота — значит, и HMAC с ним. Модуль,
# который его вводит, тянет секрет в службу (И-3).
текст = (MODULE_DIR / "tg_web_app.py").read_text(encoding="utf-8")
if "hmac" not in текст.lower():
    путь_ок("в модуле нет HMAC с токеном бота (Н-1)")
else:
    путь_плохо("в tg_web_app.py встречается hmac: проверка по токену бота запрещена (Н-1)")

итог_и_выход()
