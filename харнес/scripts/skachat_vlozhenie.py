#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скачивание вложения владельца из очереди диспетчера.

Откуда взят: написан для стартового пакета (задача 11.08.2026 «доставка текста
и вложения канала»). Раньше вложений не было вовсе: диспетчер клал в очередь
JSON сообщения с file_id, а скачивать было нечем — владелец присылал снимок
экрана, агент видел координату и не видел картинки.

    skachat_vlozhenie.py <файл-очереди.txt>   → печатает локальный путь
    skachat_vlozhenie.py --selftest           → пути разбора + больной случай

Что делает: берёт из JSON сообщения file_id, через getFile получает file_path,
качает файл в $LOG_DIR/inbox/файлы/ и печатает путь в stdout. Всё остальное —
в stderr, чтобы вызывающий брал путь как есть.

Токен НЕ передаётся в argv и не уходит в командную строку: читается из
$TG_TOKEN_FILE внутри процесса (в argv его видел бы любой в ps — К-7).

Больной случай, на котором прибор доказан: битый file_id. Telegram отвечает
ok:false с описанием — отказ должен назвать причину словами и вернуть rc≠0,
а не промолчать с пустым путём.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

СЕКУНД_НА_ЗАПРОС = 60

# Порядок важен: у фотографии есть и .photo, и (иногда) .thumbnail — брать надо
# оригинал. Внутри .photo элементы идут по возрастанию размера, поэтому нужен
# ПОСЛЕДНИЙ: первый — это превью 90×90, на нём ничего не разобрать.
ВИДЫ = ("photo", "document", "voice", "audio", "video", "video_note", "animation", "sticker")


def читать_конфиг(путь: str) -> dict:
    """Мини-парсер KEY="value". Не source: скрипту нельзя исполнять конфиг."""
    conf: dict[str, str] = {}
    try:
        with open(путь, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"""\s*([A-Z][A-Z0-9_]*)=("[^"]*"|'[^']*'|[^#\s]*)""", line)
                if m:
                    raw = m.group(2)
                    val = raw[1:-1] if raw[:1] in "\"'" else raw
                    for name, seen in conf.items():
                        val = val.replace("${%s}" % name, seen).replace("$" + name, seen)
                    conf[m.group(1)] = val
    except OSError:
        pass
    return conf


def найти_вложение(сообщение: dict) -> tuple[str, str]:
    """(file_id, вид) первого найденного вложения. Нет вложения — («», «»)."""
    for вид in ВИДЫ:
        поле = сообщение.get(вид)
        if not поле:
            continue
        # .photo — список размеров одного снимка, остальные виды — объект.
        объект = поле[-1] if isinstance(поле, list) else поле
        file_id = объект.get("file_id", "")
        if file_id:
            return file_id, вид
    return "", ""


def имя_файла(file_path: str, file_id: str, вид: str) -> str:
    """Имя на диске: расширение от Telegram, основа — от file_id.

    Имя из file_path целиком не берём: оно приходит от чужой стороны и вида
    «../../etc/passwd» разложило бы файл мимо каталога. Расширение — только
    буквы и цифры, всё остальное отбрасывается.
    """
    хвост = Path(file_path).suffix
    расширение = хвост if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", хвост) else ""
    основа = re.sub(r"[^A-Za-z0-9_-]", "_", file_id)[:64] or "вложение"
    return f"{вид}-{основа}{расширение}"


def запрос(url: str) -> dict:
    """GET к Bot API. Ответ не-JSON или сеть — исключение с человеческим текстом."""
    try:
        with urllib.request.urlopen(url, timeout=СЕКУНД_НА_ЗАПРОС) as ответ:
            тело = ответ.read()
    except urllib.error.HTTPError as e:
        # Telegram кладёт причину отказа в тело даже при 4xx — без него отказ
        # был бы «HTTP 400» без единого слова о том, что не так.
        тело = e.read()
    except urllib.error.URLError as e:
        raise RuntimeError(f"сеть недоступна: {e.reason}") from None
    try:
        return json.loads(тело.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise RuntimeError(f"Bot API ответил не JSON: {тело[:200]!r}") from None


def скачать(сообщение: dict, токен: str, каталог: Path) -> Path:
    file_id, вид = найти_вложение(сообщение)
    if not file_id:
        raise RuntimeError(
            "во входящем нет вложения: ни одного из полей " + ", ".join(ВИДЫ)
        )
    ответ = запрос(
        f"https://api.telegram.org/bot{токен}/getFile?"
        + urllib.parse.urlencode({"file_id": file_id})
    )
    if not ответ.get("ok"):
        raise RuntimeError(
            "getFile отказал: {} (код {}); file_id={}".format(
                ответ.get("description", "причина не названа"),
                ответ.get("error_code", "?"),
                file_id[:32],
            )
        )
    file_path = ответ.get("result", {}).get("file_path", "")
    if not file_path:
        raise RuntimeError(f"getFile вернул ok, но без file_path: {ответ.get('result')}")

    каталог.mkdir(parents=True, exist_ok=True)
    куда = каталог / имя_файла(file_path, file_id, вид)
    ссылка = f"https://api.telegram.org/file/bot{токен}/{urllib.parse.quote(file_path)}"
    try:
        with urllib.request.urlopen(ссылка, timeout=СЕКУНД_НА_ЗАПРОС) as поток, куда.open("wb") as ф:
            shutil.copyfileobj(поток, ф)
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"файл не скачался ({file_path}): {e}") from None
    if куда.stat().st_size == 0:
        куда.unlink(missing_ok=True)
        raise RuntimeError(f"скачан пустой файл ({file_path}) — не сохраняю")
    return куда


def настройки() -> tuple[str, Path]:
    conf = читать_конфиг(os.environ.get("HARNESS_INSTALL_CONF", "/etc/harness/install.conf"))
    файл_токена = conf.get("TG_TOKEN_FILE", "")
    if not файл_токена:
        raise RuntimeError("в install.conf пуст TG_TOKEN_FILE — качать нечем")
    try:
        токен = Path(файл_токена).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RuntimeError(f"токен не прочитан ({файл_токена}): {e}") from None
    if not токен:
        raise RuntimeError(f"файл токена пуст: {файл_токена}")
    log_dir = conf.get("LOG_DIR") or "/var/log/harness"
    return токен, Path(log_dir) / "inbox" / "файлы"


def селфтест() -> int:
    ok = True

    def проба(имя: str, ждём, факт) -> None:
        nonlocal ok
        if факт == ждём:
            print(f"  ок    {имя}")
        else:
            print(f"  ПЛОХО {имя}: ждали {ждём!r}, получили {факт!r}")
            ok = False

    # Разбор входящего: у фотографии берётся ПОСЛЕДНИЙ размер — первый это
    # превью 90×90, на котором владельцу нечего показать.
    фото = {"photo": [{"file_id": "мелкий", "width": 90}, {"file_id": "крупный", "width": 1280}]}
    проба("фото: берём последний (самый крупный) размер", ("крупный", "photo"), найти_вложение(фото))
    проба("документ", ("док1", "document"), найти_вложение({"document": {"file_id": "док1"}}))
    проба("голосовое", ("гол1", "voice"), найти_вложение({"voice": {"file_id": "гол1"}}))
    проба("текстовое сообщение — вложения нет", ("", ""), найти_вложение({"text": "привет"}))
    проба("фото важнее превью-документа", ("крупный", "photo"),
          найти_вложение({"photo": [{"file_id": "крупный"}], "document": {"file_id": "док"}}))
    # Имя файла собирается нами: путь от чужой стороны не должен уводить запись
    # из каталога — иначе вложение владельца перезаписало бы системный файл.
    проба("имя файла: путь чужой стороны не уводит из каталога",
          "photo-AgACx.jpg", имя_файла("../../etc/photos/x.jpg", "AgACx", "photo"))
    проба("имя файла: расширения нет — обходимся без него",
          "voice-ABC", имя_файла("voice/file_1", "ABC", "voice"))

    # БОЛЬНОЙ СЛУЧАЙ на живом API: битый file_id обязан дать внятный отказ с
    # причиной от Telegram, а не пустой путь и тишину.
    try:
        токен, каталог = настройки()
    except RuntimeError as e:
        print(f"  ПЛОХО больной случай не прогнан: {e}")
        return 1
    try:
        скачать({"document": {"file_id": "заведомо-битый-file-id"}}, токен, каталог)
        print("  ПЛОХО больной случай: битый file_id НЕ вызвал отказа")
        ok = False
    except RuntimeError as e:
        if "getFile отказал" in str(e) and len(str(e)) > 30:
            print(f"  ок    БОЛЬНОЙ СЛУЧАЙ: битый file_id → отказ с причиной: {e}")
        else:
            print(f"  ПЛОХО больной случай: отказ невнятный: {e}")
            ok = False

    print("SELFTEST: зелёный (8 путей: разбор, имя файла, больной случай на живом API)"
          if ok else "SELFTEST: КРАСНЫЙ")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[3].strip(), file=sys.stderr)
        return 2
    if sys.argv[1] == "--selftest":
        return селфтест()
    try:
        сырое = Path(sys.argv[1]).read_text(encoding="utf-8")
    except OSError as e:
        print(f"файл очереди не прочитан: {e}", file=sys.stderr)
        return 1
    try:
        сообщение = json.loads(сырое)
    except json.JSONDecodeError:
        print(f"{sys.argv[1]} — не JSON сообщения (текстовое входящее качать нечего)", file=sys.stderr)
        return 1
    # Диспетчер кладёт .message; на всякий случай понимаем и целый update.
    сообщение = сообщение.get("message", сообщение)
    try:
        токен, каталог = настройки()
        print(скачать(сообщение, токен, каталог))
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
