#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Законная дверь сторожа модели: назвать причину и получить 15 минут.

Сторож (scripts/hooks/model_guard.py) не даёт самой смене делать шаги трубы и
делать важный шаг чужой моделью. Запрет без достижимой двери обходят, отключая
запрет ([[a-guard-without-a-legal-door]]), поэтому дверь есть — но у неё три
свойства, и каждое названо:

* **причина обязательна** и не короче 20 знаков: «да» и «надо» не причина;
* **срок**: дверь закрывается сама, вечная дверь — это снятый сторож;
* **след**: причина уходит в журнал сторожа, её видно в ревью и в прогоне.

Переменная окружения дверью быть не может: хук запускает ОБОЛОЧКА, и её
окружение — не то, в котором агент набрал команду (замер 18.10.2026).

Прогон:
    python3 scripts/model-guard-door.py --почему 'починка самого сторожа' [--на 15]
    python3 scripts/model-guard-door.py --закрыть
    python3 scripts/model-guard-door.py --показать
    python3 scripts/model-guard-door.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config as конф                                     # noqa: E402

ПРИЧИНА_МИН = 20
СРОК_УМОЛЧАНИЕ_МИН = 15


def путь_двери() -> Path:
    return Path(os.environ.get("LOG_DIR") or конф.log_dir()) / "model-guard.дверь"


def открыть(почему: str, минут: int) -> int:
    почему = (почему or "").strip()
    if len(почему) < ПРИЧИНА_МИН:
        print(f"причина короче {ПРИЧИНА_МИН} знаков — это отписка, а не причина",
              file=sys.stderr)
        return 2
    дверь = путь_двери()
    дверь.parent.mkdir(parents=True, exist_ok=True)
    дверь.write_text(json.dumps({"почему": почему, "до": int(time.time()) + минут * 60,
                                 "кто": "смена"}, ensure_ascii=False), encoding="utf-8")
    print(f"дверь открыта на {минут} мин: {почему}")
    return 0


def показать() -> int:
    try:
        запись = json.loads(путь_двери().read_text(encoding="utf-8"))
    except Exception:
        print("дверь закрыта")
        return 1
    осталось = int(запись.get("до") or 0) - int(time.time())
    if осталось <= 0:
        print("дверь просрочена — считается закрытой")
        return 1
    print(f"дверь открыта ещё {осталось // 60} мин: {запись.get('почему')}")
    return 0


def selftest() -> int:
    import tempfile
    плохо = 0
    with tempfile.TemporaryDirectory() as врем:
        os.environ["LOG_DIR"] = врем
        случаи = [
            ("отписка дверь не открывает", 2, открыть("да", 15)),
            ("причина открывает", 0, открыть("чиню сам сторож, помощника звать нечем", 15)),
            ("открытая дверь видна", 0, показать()),
        ]
        путь_двери().write_text(json.dumps(
            {"почему": "просроченная", "до": int(time.time()) - 60}, ensure_ascii=False),
            encoding="utf-8")
        случаи.append(("просроченная дверь считается закрытой", 1, показать()))
        for имя, ждём, вышло in случаи:
            if ждём == вышло:
                print(f"  ок    {имя}")
            else:
                плохо += 1
                print(f"  ПЛОХО {имя}: ждали {ждём}, вышло {вышло}")
    print(f"ДВЕРЬ СТОРОЖА МОДЕЛИ: путей {len(случаи)}, неудач {плохо}")
    return 1 if плохо else 0


def main() -> int:
    р = argparse.ArgumentParser(add_help=True)
    р.add_argument("--почему")
    р.add_argument("--на", type=int, default=СРОК_УМОЛЧАНИЕ_МИН)
    р.add_argument("--закрыть", action="store_true")
    р.add_argument("--показать", action="store_true")
    р.add_argument("--selftest", action="store_true")
    а = р.parse_args()
    if а.selftest:
        return selftest()
    if а.закрыть:
        путь_двери().unlink(missing_ok=True)
        print("дверь закрыта")
        return 0
    if а.показать:
        return показать()
    if а.почему:
        return открыть(а.почему, а.на)
    р.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
