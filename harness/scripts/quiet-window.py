#!/usr/bin/env python3
"""Открыто ли окно тишины продукта. Код 0 — открыто, 1 — нет или неизвестно.

Путь к состоянию приёмника тревог — довод. Разбор отдельным файлом, а не
строкой внутри демона: вложенный heredoc закрывает внешний, и правка молча
уезжает не туда (поймано 14.10.2026).
"""
import datetime
import json
import sys


def окно_открыто(путь: str, сейчас: datetime.datetime | None = None) -> bool:
    try:
        with open(путь, encoding="utf-8") as фх:
            окно = (json.load(фх) or {}).get("window")
    except (OSError, ValueError):
        return False
    до = (окно or {}).get("until")
    if not до:
        return False
    try:
        край = datetime.datetime.fromisoformat(str(до).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    теперь = сейчас or datetime.datetime.now(край.tzinfo)
    return край > теперь


def _selftest() -> int:
    import tempfile
    from pathlib import Path
    плохо = 0

    def проба(имя, ждём, факт):
        nonlocal плохо
        if факт == ждём:
            print(f"  ок    {имя}")
        else:
            print(f"  ПЛОХО {имя}: ждали {ждём}, вышло {факт}")
            плохо = 1

    with tempfile.TemporaryDirectory() as двор:
        д = Path(двор)
        поздно = "2099-01-01T00:00:00+00:00"
        рано = "2000-01-01T00:00:00+00:00"
        (д / "open.json").write_text(json.dumps(
            {"window": {"until": поздно, "why": "выкат"}}), encoding="utf-8")
        (д / "past.json").write_text(json.dumps(
            {"window": {"until": рано, "why": "выкат"}}), encoding="utf-8")
        (д / "none.json").write_text(json.dumps({"states": {}}), encoding="utf-8")
        (д / "broken.json").write_text("{не json", encoding="utf-8")
        проба("окно до будущего — открыто", True, окно_открыто(str(д / "open.json")))
        # БОЛЬНОЙ СЛУЧАЙ: окно ПРОСРОЧЕНО. Незакрытое окно в файле не должно
        # глушить сторожа вечно — выкат мог упасть, не закрыв его.
        проба("БОЛЬНОЙ: просроченное окно не глушит", False,
              окно_открыто(str(д / "past.json")))
        проба("окна в файле нет — не открыто", False, окно_открыто(str(д / "none.json")))
        # БОЛЬНОЙ СЛУЧАЙ: битый файл — это НЕ «окно открыто». Иначе один
        # испорченный байт заставил бы сторожа молчать о настоящей беде.
        проба("БОЛЬНОЙ: битый файл не глушит", False, окно_открыто(str(д / "broken.json")))
        проба("файла нет — не глушит", False, окно_открыто(str(д / "нет-такого.json")))
    print("SELFTEST: " + ("зелёный (5 путей, среди них просроченное окно и битый файл)"
                          if not плохо else "КРАСНЫЙ"))
    return плохо


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(0 if len(sys.argv) > 1 and окно_открыто(sys.argv[1]) else 1)
