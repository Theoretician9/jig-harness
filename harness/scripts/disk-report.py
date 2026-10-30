#!/usr/bin/env python3
"""disk-report.py — решение тревоги «диск занят» и опись «где лежит место».

Ничего не удаляет. Метка `$HEARTBEAT_DIR/disk-alert` — JSON {"процент": N, "когда": "<ISO с поясом>"};
её пишет только `--отправлено`, и только после того, как канал принял весть: недоставленная весть
обязана повториться.

    --тревога --занято N --освобождено МБ   первая строка решения, код 0 — шлём весть (текст ниже),
                                            код 3 — молчим; другой код — поломка
    --отправлено --занято N                 весть ушла: записать метку
    --опись                                 строки «класс<TAB>МБ<TAB>чьё<TAB>решение» и итог
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402
import yaml  # noqa: E402

GROW_PP = 3
DEFAULT_MAX_PCT = "85"
DEFAULT_REPEAT_H = "24"
MARK_NAME = "disk-alert"
DU_ARGS = ("du", "-x", "-k", "--max-depth=3", "/")
# Мягкий потолок канала из TONE.md: сторож принимает и больше, но владелец читает короткое.
TEXT_LIMIT = 700
WAITING = "ждёт слова"
NOT_VISIBLE = "под правом агента: чужое не видно"
EXIT_SILENT = 3
EXIT_USAGE = 2


class Mark(NamedTuple):
    pct: int
    when: float


class Unreadable:
    """Метка есть, но прочесть её нельзя: для решения это «метки нет», для отчёта — отдельная причина."""


class Decision(NamedTuple):
    kind: str
    reason: str


class Row(NamedTuple):
    klass: str
    mb: int
    whose: str
    decision: str


def read_mark(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return Unreadable()
    try:
        data = json.loads(raw)
        pct = data["процент"]
        when = dt.datetime.fromisoformat(data["когда"])
    except (ValueError, KeyError, TypeError):
        return Unreadable()
    if not isinstance(pct, int) or isinstance(pct, bool) or when.tzinfo is None:
        return Unreadable()
    return Mark(pct, when.timestamp())


BELOW = {
    "нет": Decision("ниже порога", ""),
    "нечитаема": Decision("перевзвод", "метка снята"),
    "есть": Decision("перевзвод", "метка снята"),
}
ABOVE_WITHOUT_MARK = {
    "нет": Decision("весть", "метки нет"),
    "нечитаема": Decision("весть", "метка нечитаема"),
}


def decide(pct, mark, now, max_pct, repeat_h, grow_pp=GROW_PP) -> Decision:
    mark_kind = "нет" if mark is None else "нечитаема" if isinstance(mark, Unreadable) else "есть"
    if pct < max_pct:
        return BELOW[mark_kind]
    if mark_kind != "есть":
        return ABOVE_WITHOUT_MARK[mark_kind]
    if now - mark.when >= repeat_h * 3600:
        return Decision("весть", f"прошло {repeat_h:g} ч")
    if pct - mark.pct >= grow_pp:
        return Decision("весть", f"рост на {pct - mark.pct} п.п.")
    return Decision("молчу", "")


def du_sizes() -> tuple:
    """КБ по путям. Под `sudo -n` видно чужое; без права опись честно говорит, что видит меньше."""
    for prefix, note in ((("sudo", "-n"), ""), ((), NOT_VISIBLE)):
        out = subprocess.run([*prefix, *DU_ARGS], capture_output=True, text=True, timeout=900).stdout
        sizes = {}
        for line in out.splitlines():
            kb, _, path = line.partition("\t")
            if kb.isdigit():
                sizes[path] = int(kb)
        if sizes:
            return sizes, note
    return {}, NOT_VISIBLE


def load_classes(settings: dict) -> list:
    """Классы описи — данные раздела «опись:»; `$КЛЮЧ` в пути подставляется из настроек."""
    path = config.корень_дерева() / "harness" / "config" / "tmp-sweep.yaml"
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))["опись"]
    classes = []
    for entry in entries:
        place = entry["путь"]
        for key, value in settings.items():
            place = place.replace(f"${key}", value)
        if "$" not in place:
            classes.append((place, entry["класс"], entry["чьё"], entry["решение"]))
    return classes


def inventory(sizes: dict, classes: list, top: int) -> list:
    rows = [Row(klass, sizes[place] // 1024, whose, decision)
            for place, klass, whose, decision in classes if place in sizes]
    claimed = [place for place, *_ in classes if place in sizes]
    inside = lambda p: any(p == c or p.startswith(c.rstrip("/") + "/") for c in claimed)  # noqa: E731
    has_child = lambda p: any(q.startswith(p.rstrip("/") + "/") for q in sizes)  # noqa: E731
    leaves = sorted((p for p in sizes if p != "/" and not inside(p) and not has_child(p)),
                    key=lambda p: -sizes[p])
    rows += [Row(p, sizes[p] // 1024, "чьё — не знаю", WAITING) for p in leaves[:top]]
    return sorted(rows, key=lambda r: -r.mb)


def gb(mb: float) -> str:
    return f"{mb / 1024:.1f}".replace(".", ",")


def _listing(rows) -> str:
    return ", ".join(f"{r.klass} {gb(r.mb)} ГБ" for r in rows)


def alert_text(pct, freed_mb, rows, repeat_h=24) -> str:
    rows = list(rows)

    def compose() -> str:
        own = [r for r in rows if r.decision != WAITING]
        waiting = [r for r in rows if r.decision == WAITING]
        lines = [f"Диск занят на {pct} %. Уборка убрала {gb(freed_mb)} ГБ."]
        if own:
            lines.append(f"Убирается само: {_listing(own)}.")
        if waiting:
            lines.append(f"Ждут твоего слова: {_listing(waiting)}.")
        lines.append(f"Повторю через {repeat_h:g} ч, раньше — если вырастет на {GROW_PP} %.")
        return "\n".join(lines)

    while len(compose()) > TEXT_LIMIT and rows:
        rows.remove(min(rows, key=lambda r: r.mb))
    return compose()


def argument(name: str) -> str:
    return sys.argv[sys.argv.index(name) + 1]


def settings() -> dict:
    """harness.conf (окружение старше файла) поверх паспорта установки."""
    return {**config.конфиг(config.путь_harness_conf()), **config.конфиг()}


def write_mark(path: Path, pct: int) -> None:
    now = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"процент": pct, "когда": now}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def alarm(cfg: dict, mark_file: Path) -> int:
    pct, freed = int(argument("--занято")), int(argument("--освобождено"))
    # Умолчание ПОСЛЕ загрузчика: ключа в живом harness.conf может не быть до первого install-new.
    repeat_h = float(cfg.get("DISK_ALERT_REPEAT_H") or DEFAULT_REPEAT_H)
    max_pct = int(cfg.get("DISK_MAX_PCT") or DEFAULT_MAX_PCT)
    mark = read_mark(mark_file)
    verdict = decide(pct, mark, time.time(), max_pct, repeat_h)
    if verdict.kind == "перевзвод":
        mark_file.unlink(missing_ok=True)
    if verdict.kind == "молчу":
        at = dt.datetime.fromtimestamp(mark.when).strftime("%H:%M")
        print(f"молчу: тревога уже отправлена в {at} при {mark.pct} %")
    elif verdict.kind == "весть":
        sizes, _ = du_sizes()
        print(f"весть: {verdict.reason}")
        print(alert_text(pct, freed, inventory(sizes, load_classes(cfg), top=4), repeat_h))
        return 0
    else:
        print(f"{verdict.kind}: {verdict.reason}" if verdict.reason else verdict.kind)
    return EXIT_SILENT


def listing(cfg: dict) -> int:
    sizes, note = du_sizes()
    rows = inventory(sizes, load_classes(cfg), top=8)
    if note:
        print(note)
    for r in rows:
        print(f"{r.klass}\t{r.mb}\t{r.whose}\t{r.decision}")
    print(f"опись: {len(rows)} классов, {sum(r.mb for r in rows)} МБ")
    return 0


def main() -> int:
    cfg = settings()
    mark_file = Path(config.heartbeat_dir()) / MARK_NAME
    if "--отправлено" in sys.argv:
        write_mark(mark_file, int(argument("--занято")))
        return 0
    if "--тревога" in sys.argv:
        return alarm(cfg, mark_file)
    if "--опись" in sys.argv:
        return listing(cfg)
    print("ключи: --тревога --занято N --освобождено МБ | --отправлено --занято N | --опись",
          file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
