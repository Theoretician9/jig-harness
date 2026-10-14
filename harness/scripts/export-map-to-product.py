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

КУДА. След лежит в РЕПОЗИТОРИИ продукта (PRODUCT_REPO в harness.conf), а не
в его каталоге на машине (PRODUCT_PATH): у продукта, чей compose вложен в
репозиторий (`lama-cms/deploy`), это разные места, и след от PRODUCT_PATH ложился
в `deploy/src/docs/` мимо репозитория. Ключ пуст — отказ словами, а не запись
в выдуманный каталог. Тот же путь читает импорт следа (import-product-trace.py).

НЕ ЗАТИРАЕТ НЕСВЕРЕННОЕ. След бывает единственным носителем карточек: продукт
приехал на сервер без своей карты, и пока след не импортирован
(import-product-trace.py), перезапись оставила бы от 130 карточек две. Поэтому
карточка, которая есть в следе, но нет ни в карте, ни в архиве, — отказ словами.

Запуск: python3 scripts/export-map-to-product.py [--куда <путь>] [--selftest]
"""
import argparse
import re
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
MAP_IN_PRODUCT = "docs/КАРТА-РАЗРАБОТКИ.md"
PRODUCT_PREFIX = "pr."
# Заголовки разделов следа по статусу. Их же читает импорт следа обратно в
# карту — один словарь на писателя и читателя.
STATUS_ORDER = ["wip", "test", "plan", "беклог", "done"]
STATUS_HEADINGS = {"wip": "В работе", "test": "Ждут проверки владельца или живой",
                   "plan": "Запланировано", "беклог": "Беклог — не трогать без его слова",
                   "done": "Закрыто"}


TRACE_CARD = re.compile(r"^### `(.+)`\s*$", re.M)
ARCHIVE = "docs/dev-map-archive.yaml"


def lost_by_overwrite(trace_text: str, kept: set) -> list[str]:
    """id из следа, которых нет ни в карте, ни в архиве: перезапись их потеряет."""
    return sorted(set(TRACE_CARD.findall(trace_text)) - kept)


def trace_path(settings: dict) -> Path | None:
    """Путь следа в репозитории продукта; None — PRODUCT_REPO не задан."""
    repo = (settings.get("PRODUCT_REPO") or "").strip()
    return Path(repo) / MAP_IN_PRODUCT if repo else None


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
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    куда = getattr(args, "куда")
    if not куда:
        default = trace_path(конфиг(путь_harness_conf()))
        if default is None:
            print("в harness.conf пуст PRODUCT_REPO — куда писать след, "
                  "неизвестно; задайте ключ или позовите с --куда <путь>",
                  file=sys.stderr)
            return 2
        куда = str(default)
    out = Path(куда)

    data = yaml.safe_load((ROOT / "dev-map.yaml").read_text(encoding="utf-8"))
    all_cards = list(cards(data))
    if out.exists():
        kept = {str(c["id"]) for c in all_cards}
        if (ROOT / ARCHIVE).exists():
            kept |= {str(c["id"]) for c in cards(yaml.safe_load(
                (ROOT / ARCHIVE).read_text(encoding="utf-8")))}
        lost = lost_by_overwrite(out.read_text(encoding="utf-8"), kept)
        if lost:
            print(f"ОТКАЗ: в следе {out} карточек нет ни в карте, ни в архиве — {len(lost)} "
                  f"(например {', '.join(lost[:3])}). Перезапись их потеряет; сначала "
                  f"python3 scripts/import-product-trace.py", file=sys.stderr)
            return 1
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

    for status in STATUS_ORDER + [s for s in by_status if s not in STATUS_ORDER]:
        rows = by_status.get(status) or []
        if not rows:
            continue
        lines.append(f"## {STATUS_HEADINGS.get(status, status)} — {len(rows)}")
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


def selftest() -> int:
    """Путь следа — от репозитория продукта, не от его каталога на машине."""
    cases = [
        ({"PRODUCT_PATH": "/srv/shop/deploy", "PRODUCT_REPO": "/srv/shop"},
         Path("/srv/shop/docs/КАРТА-РАЗРАБОТКИ.md"),
         "БОЛЬНОЙ СЛУЧАЙ: compose вложен в репозиторий (lama-cms/deploy) — след в docs/ репозитория"),
        ({"PRODUCT_PATH": "/srv/shop", "PRODUCT_REPO": "/srv/shop"},
         Path("/srv/shop/docs/КАРТА-РАЗРАБОТКИ.md"),
         "каталог продукта и репозиторий совпадают — тот же путь"),
        ({"PRODUCT_PATH": "/srv/shop/deploy", "PRODUCT_REPO": " "}, None,
         "PRODUCT_REPO пуст — отказ словами, а не каталог от PRODUCT_PATH"),
    ]
    trace = "### `pr.a`\n**A**\n\n### `pr.b`\n**B**\n"
    overwrite = [
        (trace, {"pr.first"}, ["pr.a", "pr.b"],
         "БОЛЬНОЙ СЛУЧАЙ: импорта ещё не было — в карте 1 карточка, в следе 2 других: отказ"),
        (trace, {"pr.a", "pr.b", "pr.first"}, [], "всё из следа есть в карте или архиве — пишем"),
    ]
    ok = True
    for text, kept, expected, why in overwrite:
        got = lost_by_overwrite(text, kept)
        ok &= got == expected
        print(f"  {'ок   ' if got == expected else 'ПЛОХО'} {why} (потеряет {got})")
    for settings, expected, why in cases:
        got = trace_path(settings)
        ok &= got == expected
        print(f"  {'ок   ' if got == expected else 'ПЛОХО'} {why} (получено {got})")
    print(f"САМОТЕСТ {'ПРОЙДЕН' if ok else 'ПРОВАЛЕН'}: {len(cases) + len(overwrite)} путей, два больных случая")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
