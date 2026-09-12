#!/usr/bin/env python3
"""Гейт: предложения ревизии памяти не висят открытыми дольше срока.

Замечание владельца П-5 к ревизии 10.09.2026: «демон, чьи предложения никто не
обязан исполнять, — половина механизма; вторая половина — принуждение, и оно
должно быть кодом». Улика: ревизия 01.09 прислала владельцу четыре части
предложений, и через девять дней не выполнено ни одно.

Открытое предложение старше 14 дней (СРОК в этом файле) красит ворота. Закрыть можно только
назвав, ЧТО сделано, — иначе принуждение вырождается в кнопку «отстань».

    python3 scripts/check-predlozheniya.py                 # проверка (ворота)
    python3 scripts/check-predlozheniya.py --список
    python3 scripts/check-predlozheniya.py --закрыть a1b2c3 --как "даты проставлены"
    python3 scripts/check-predlozheniya.py --selftest
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from konf import log_dir, конфиг  # noqa: E402 — один парсер конфига на все гейты

СРОК = 14


def файл() -> str:
    return os.path.join(log_dir(), "предложения.jsonl")


def читать() -> list:
    """Битая строка — находка, а не тишина: потеря записи должна быть видна."""
    строки, битых = [], 0
    try:
        with open(файл(), encoding="utf-8") as fh:
            for l in fh:
                if not l.strip():
                    continue
                try:
                    строки.append(json.loads(l))
                except Exception:
                    битых += 1
    except OSError:
        pass
    if битых:
        print(f"[предложения] ⚠ журнал повреждён: непрочитанных строк {битых}",
              file=sys.stderr)
    return строки


def писать(строки: list) -> None:
    """Замена файла ЦЕЛИКОМ — атомарно и под локом.

    Ревью 10.09.2026: журнал дописывает ежемесячная ревизия из cron, а закрытие
    предложения перечитывало файл и писало назад открытым `w`. Дописанное между
    чтением и записью пропадало молча, а гейт после потери зеленел; падение
    между открытием и записью оставляло файл пустым.
    """
    цель = файл()
    with open(цель + ".lock", "w") as лок:
        fcntl.flock(лок, fcntl.LOCK_EX)
        врем = цель + ".tmp"
        with open(врем, "w", encoding="utf-8") as fh:
            for d in строки:
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(врем, цель)


def просроченные(строки: list, срок: int = СРОК, сегодня: str | None = None) -> list:
    порог = (datetime.date.fromisoformat(сегодня or datetime.date.today().isoformat())
             - datetime.timedelta(days=срок)).isoformat()
    return [d for d in строки if d.get("статус") == "открыто" and d.get("дата", "9999") < порог]


def закрыть(строки: list, ид: str, как: str) -> int:
    """Закрытие предложения с ответом «что сделано». Без ответа — отказ."""
    if not как:
        print("нужно --как «что именно сделано»: закрытие без ответа — кнопка «отстань»")
        return 1
    нашли = False
    for d in строки:
        if d.get("id") == ид:
            d["статус"] = "закрыто"
            d["как"] = как
            d["закрыто"] = datetime.date.today().isoformat()
            нашли = True
    писать(строки)
    print(f"предложение {ид}: {'закрыто' if нашли else 'НЕ НАЙДЕНО'}")
    return 0 if нашли else 1


def показать(строки: list) -> int:
    открытые = [d for d in строки if d.get("статус") == "открыто"]
    print(f"=== предложений ревизии памяти: открытых {len(открытые)} из {len(строки)} ===")
    for d in открытые:
        print(f"  {d.get('id', '?')} · {d.get('дата', '?')} · "
              f"{str(d.get('текст', '(текста нет)'))[:100]}")
    return 0


def гейт(строки: list) -> int:
    """Ворота: просроченное предложение — красный с перечнем и способом закрыть."""
    открытые = [d for d in строки if d.get("статус") == "открыто"]
    просроч = просроченные(строки)
    if not просроч:
        print(f"[предложения] открытых {len(открытые)}, просроченных нет")
        return 0
    print(f"[предложения] ✗ висят дольше {СРОК} дн: {len(просроч)}", file=sys.stderr)
    for d in просроч[:5]:
        print(f"      • {d.get('id', '?')} ({d.get('дата', '?')}): "
              f"{str(d.get('текст', ''))[:90]}", file=sys.stderr)
    print("      закрыть: python3 scripts/check-predlozheniya.py --закрыть <id> --как «...»",
          file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--список", action="store_true")
    ap.add_argument("--закрыть")
    ap.add_argument("--как", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return самотест()
    строки = читать()
    if a.закрыть:
        return закрыть(строки, a.закрыть, a.как)
    return показать(строки) if a.список else гейт(строки)


def самотест() -> int:
    ok = True
    свежее = [{"id": "a", "дата": datetime.date.today().isoformat(), "текст": "т", "статус": "открыто"}]
    старое = [{"id": "b", "дата": "2026-01-01", "текст": "т", "статус": "открыто"}]
    закрытое = [{"id": "c", "дата": "2026-01-01", "текст": "т", "статус": "закрыто"}]
    случаи = [
        ("БОЛЬНОЙ СЛУЧАЙ: открытое предложение старше срока — просрочено",
         len(просроченные(старое)) == 1),
        ("свежее открытое — не просрочено", len(просроченные(свежее)) == 0),
        ("закрытое старое — не просрочено", len(просроченные(закрытое)) == 0),
        ("граница срока принадлежит здоровью",
         len(просроченные([{"id": "d", "дата": "2026-09-01", "текст": "т", "статус": "открыто"}],
                          срок=14, сегодня="2026-09-15")) == 0),
    ]
    for имя, годен in случаи:
        print(f"  {'ок   ' if годен else 'ПЛОХО'} {имя}")
        ok = ok and годен
    print("SELFTEST: зелёный (4 пути, первым — больной случай)" if ok else "SELFTEST: КРАСНЫЙ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
