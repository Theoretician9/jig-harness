"""Проверка входа Telegram web_app: подпись Ed25519 по схеме «для третьих лиц».

Токена бота у панели нет и не будет: Telegram подписывает initData своим
ключом, а панель знает только его открытую часть и номер бота (TG_BOT_ID,
TG_WEBAPP_PUBKEY в harness.conf). Подпись судит библиотека (RFC 8032), своей
арифметики здесь нет.
"""
import base64
import json
import urllib.parse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _signed_string(fields: dict, bot_id: str) -> bytes:
    """Строка, которую подписал Telegram: номер бота и поля по алфавиту."""
    строки = (f"{k}={v}" for k, v in sorted(fields.items()))
    return (f"{bot_id}:WebAppData\n" + "\n".join(строки)).encode()


def _signature(текст: str) -> bytes:
    """base64url без «=»: выравнивание дописываем сами."""
    return base64.urlsafe_b64decode(текст + "=" * (-len(текст) % 4))


def verify(init_data: str, *, bot_id: str, pubkey_hex: str, owner_id: str,
           max_age_s: int, now: float) -> bool:
    """True, только если подпись верна, данные свежи и пользователь — владелец.

    Любая ошибка разбора — False: вход не должен падать исключением.
    """
    if not bot_id or not pubkey_hex:
        return False
    try:
        fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True,
                                             strict_parsing=True))
        fields.pop("hash", None)  # подпись старой схемы по токену бота: не нужна и не мешает
        signature = _signature(fields.pop("signature"))
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        key.verify(signature, _signed_string(fields, bot_id))
        if now - int(fields["auth_date"]) > max_age_s:
            return False
        return json.loads(fields["user"])["id"] == int(owner_id)
    except Exception:
        return False
