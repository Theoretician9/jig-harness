#!/usr/bin/env python3
"""Суд итога ворот с карантином: красны только НОВЫЕ красные и беды самого карантина.

Договор — .tasks/hr.quarantine-red-checks/спека.md. Вердикты прогона берутся из
журнала gates-timing.jsonl (строки gate.check с data.run == --прогон), число
красных — от ворот (--красных): красный без строки журнала всегда новый.
Код выхода: min(новых + бед карантина, 255); нет файла или список пуст — молчим,
код min(красных, 255). Суд упал сам — код 1: падение не должно сойти за зелёное.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import cards  # noqa: E402

MAX_DAYS = 7


def red_gates(journal: Path, run: str) -> tuple[set, set]:
    """(красные, зелёные) имена гейтов этого прогона; чужие прогоны не в счёт."""
    red, green = set(), set()
    for line in journal.read_text(encoding="utf-8").splitlines():
        # Журнал — наблюдение (harness_event не роняет ворота и целых строк не
        # обещает): битая строка не вердикт, а красный без своей строки и так
        # новый — счёт красных идёт от RED ворот, не от журнала.
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        data = rec.get("data") or {}
        if rec.get("event") != "gate.check" or data.get("run") != run:
            continue
        # gates_check.sh пишет outcome только ok|refused: всё, что не refused, — ok.
        (red if rec.get("outcome") == "refused" else green).add(data.get("name"))
    return red, green


def load_entries(path: Path) -> list:
    """Записи карантина; файла нет — пусто."""
    if not path.is_file():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("карантин") or []


def defect(entry: dict, today: date, card_ids: set, names: list) -> str:
    """Почему запись негодна (пусто — годна)."""
    try:
        made = date.fromisoformat(str(entry.get("заведено")))
        due = date.fromisoformat(str(entry.get("срок")))
    except ValueError:
        return "дата не разбирается"
    if due < today:
        return f"срок вышел {due}"
    if (due - made).days > MAX_DAYS:
        return f"срок {made}..{due} длиннее {MAX_DAYS} дней"
    if not entry.get("карточка"):
        return "нет карточки"
    if str(entry["карточка"]) not in card_ids:
        return "карточки нет в карте"
    if names.count(entry.get("гейт")) > 1:
        return "две записи на один гейт"
    return ""


def judge(args) -> int:
    entries = load_entries(Path(args.quarantine))
    if not entries:
        return min(args.red, 255)
    today = date.fromisoformat(args.today) if args.today else date.today()
    red, green = red_gates(Path(args.journal), args.run)
    devmap = yaml.safe_load(Path(args.devmap).read_text(encoding="utf-8")) or {}
    card_ids, names = cards.task_ids(devmap), [e.get("гейт") for e in entries]
    covered, bad, seen = 0, 0, set()
    for e in entries:
        gate, card = e.get("гейт"), e.get("карточка")
        if gate in seen:  # дубль — одна беда на гейт, она уже названа у первой записи
            continue
        seen.add(gate)
        why = defect(e, today, card_ids, names)
        if why:
            bad += 1
            print(f"  ПЛОХО · карантин: {gate} — {why} (карточка {card})")
        elif gate in red:
            covered += 1
            print(f"  карантин · {gate} — красный, карточка {card}, до {e['срок']}")
        elif gate in green:
            print(f"  карантин можно снять · {gate} — зелёный в этом прогоне (карточка {card})")
    if covered > args.red:  # журнал и ворота считали разное — прикрытию не верим
        bad += 1
        print(f"  ПЛОХО · карантин: счёт расходится — под карантином {covered}, "
              f"а красных у ворот {args.red}")
        covered = 0
    new = args.red - covered
    print(f"=== карантин: красных всего {args.red}, в карантине {covered}, "
          f"новых красных {new}, бед карантина {bad} ===")
    return min(new + bad, 255)


def main() -> int:
    ap = argparse.ArgumentParser()
    for flag, dest in (("--карантин", "quarantine"), ("--карта", "devmap"), ("--журнал", "journal"),
                       ("--прогон", "run"), ("--сегодня", "today")):
        ap.add_argument(flag, dest=dest)
    ap.add_argument("--красных", dest="red", type=int, required=True)
    try:
        return judge(ap.parse_args())
    except Exception as err:  # суд упал — это красный, а не молчание
        print(f"ПЛОХО · суд карантина упал: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
