#!/usr/bin/env python3
"""Выгрузка карты разработки в репозиторий ПРОДУКТА.

ЗАЧЕМ. Владелец 13.10.2026 велел держать текущую карту разработки и в
репозитории продукта. Карта живёт в репозитории ХАРНЕСА и уезжает на новый
сервер вместе с ним, а репозиторий продукта он даёт агенту ОТДЕЛЬНО — и смена,
поднятая на одном лишь коде продукта, не знает ни что сделано, ни где работа
остановлена.

ПОЧЕМУ ВЫГРУЗКА, А НЕ ВТОРАЯ КАРТА. Два файла одной правды расходятся молча
(13.10 этим же днём так разъехались два образца `.env`). Источник правды —
`dev-map.yaml` харнеса; здесь СЛЕД этого источника с отметкой времени, и
читатель видит, насколько след свеж.

КУДА. Путь к продукту — ДАННЫЕ (PRODUCT_PATH в harness.conf), а не литерал:
вшитый путь это след нашей установки в общем инструменте. Ключ пуст — отказ
словами, а не запись в выдуманный каталог.

Запуск: python3 scripts/export-map-to-product.py [--куда <путь>]
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
from config import конфиг, путь_harness_conf  # noqa: E402

# Где внутри репозитория продукта лежит след. Относительный путь — не след
# установки: так устроен репозиторий, а не машина.
MAP_IN_PRODUCT = "src/docs/КАРТА-РАЗРАБОТКИ.md"
PRODUCT_PREFIX = "pr."


def cards(node):
    """Все карточки карты, где бы они ни лежали в дереве."""
    if isinstance(node, dict):
        if "id" in node and "status" in node:
            yield node
        for value in node.values():
            yield from cards(value)
    elif isinstance(node, list):
        for value in node:
            yield from cards(value)


def harness_commit() -> str:
    """Коммит харнеса, по которому сделан след. «Не прочитали» ≠ «нет»."""
    answer = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, check=False)
    return answer.stdout.strip() if answer.returncode == 0 else "не прочитан"


def state_point() -> str:
    """«Где мы» из STATE.md — первый абзац раздела «ЧТО ДЕЛАТЬ ПЕРВЫМ ДЕЛОМ»."""
    state = (ROOT / "STATE.md").read_text(encoding="utf-8")
    mark = "## ЧТО ДЕЛАТЬ ПЕРВЫМ ДЕЛОМ"
    if mark not in state:
        return "в STATE.md нет раздела «ЧТО ДЕЛАТЬ ПЕРВЫМ ДЕЛОМ» — точку остановки читать там же"
    tail = state.split(mark, 1)[1]
    return tail.split("\n## ", 1)[0].strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--куда", default="")
    args = parser.parse_args()
    куда = getattr(args, "куда")
    if not куда:
        корень = (конфиг(путь_harness_conf()).get("PRODUCT_PATH") or "").strip()
        if not корень:
            print("в harness.conf пуст PRODUCT_PATH — куда писать след, "
                  "неизвестно; задайте ключ или позовите с --куда <путь>",
                  file=sys.stderr)
            return 2
        куда = str(Path(корень) / MAP_IN_PRODUCT)
    out = Path(куда)

    data = yaml.safe_load((ROOT / "dev-map.yaml").read_text(encoding="utf-8"))
    all_cards = list(cards(data))
    mine = [c for c in all_cards if str(c.get("id", "")).startswith(PRODUCT_PREFIX)]
    if not mine:
        print("в карте нет ни одной карточки продукта — выгружать нечего", file=sys.stderr)
        return 1

    by_status: dict[str, list[dict]] = {}
    for card in mine:
        by_status.setdefault(str(card.get("status")), []).append(card)

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        "# Карта разработки — след карты харнеса",
        "",
        f"> Снято {now} с карты харнеса, коммит `{harness_commit()}`.",
        "> **Источник правды — `dev-map.yaml` в репозитории харнеса**, здесь след:",
        "> два файла одной правды расходятся молча, поэтому правится карта, а не этот файл.",
        "> Пересоздать: `python3 scripts/export-map-to-product.py` (харнес).",
        "",
        "## Где остановлена работа",
        "",
        state_point(),
        "",
        "## Очередь работ",
        "",
    ]
    queue = data.get("queue") or []
    lines += [f"{i}. `{task}`" for i, task in enumerate(queue, 1)] or ["Очередь пуста."]
    lines.append("")

    порядок = ["wip", "test", "plan", "беклог", "done"]
    имена = {"wip": "В работе", "test": "Ждут проверки владельца или живой",
             "plan": "Запланировано", "беклог": "Беклог — не трогать без его слова",
             "done": "Закрыто"}
    for status in порядок + [s for s in by_status if s not in порядок]:
        rows = by_status.get(status) or []
        if not rows:
            continue
        lines.append(f"## {имена.get(status, status)} — {len(rows)}")
        lines.append("")
        if status == "done":
            lines.append(f"Закрытых карточек продукта: {len(rows)}. Их список — в карте харнеса.")
            lines.append("")
            continue
        for card in sorted(rows, key=lambda c: str(c.get("id"))):
            lines.append(f"### `{card['id']}`")
            lines.append(f"**{card.get('title', '—')}**")
            lines.append("")
            lines.append(str(card.get("summary", "")).strip() or "—")
            how = str(card.get("как_проверить", "")).strip()
            if how:
                lines.append("")
                lines.append(f"*Чем проверить:* {how}")
            lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"след карты записан: {out} · карточек продукта {len(mine)}, "
          f"из них в работе {len(by_status.get('wip', []))}, "
          f"ждут проверки {len(by_status.get('test', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
