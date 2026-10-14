#!/usr/bin/env python3
"""Импорт следа карты продукта обратно в карту харнеса — одна правда.

ЗАЧЕМ. `docs/КАРТА-РАЗРАБОТКИ.md` продукта — СЛЕД, снятый
`export-map-to-product.py` с карты харнеса. Когда продукт приезжает на новый
сервер без своей прежней карты, след — единственный носитель его карточек, и
доска владельца без них показывает задачи продукта неполными (лама-cms 14.10:
2 карточки на доске из 134 в следе). Этот скрипт кладёт карточки следа в линию
слоя `product` карты — и источник снова один.

ЧТО ПЕРЕНОСИТСЯ. id, title, summary, «Чем проверить» (→ `как_проверить`) и
статус по разделу следа. Остальные поля карточки экспорт не пишет — их нет.
  • Карточки-реплики канала (автозаведение map-from-message.py: summary
    начинается «Владелец <дата> в канал: «…») — не задачи: они уходят в архив
    карты с причиной, а не на доску. Данные целы, доска честна.
  • Закрытые след отдаёт ЧИСЛОМ, без id. Их id восстанавливаются из истории
    следа в git: карточка была в прежней ревизии и пропала из текущей — её
    закрыли. Не `done`: подтверждение владельца осталось на старом сервере (И-4).
  • След снят со СТАРОГО сервера разработки, а этот сервер боевой. Владелец
    14.10: «прежде чем задачи брать сделай их ревизию». Поэтому каждая карточка
    приходит с `blocked: "ждёт ревизии…"` (доска показывает его на лице карточки,
    очередь такую не берёт), а `test` следа становится `plan`: «ждёт проверки»
    на доске значит только проверку на ЭТОМ сервере. Прежний статус — в поле
    `статус_в_следе`. Ревизия снимает blocked и ставит статус по итогу.

ЧЕГО НЕ ДЕЛАЕТ. Не трогает карточку с уже существующим id (в карте или архиве)
— ни заголовок, ни статус: повтор и наложение ничего не перетирают.

КАК ПИШЕТ. Под общим локом карты dev-map.lock (её пишут несколько сторон),
вставкой текста перед якорем — не пересборкой YAML, чтобы не тронуть ни одной
чужой строки. Итог сверяется разбором: прежние id все на месте, новых ровно
столько, сколько добавляли, и каждая карточка разбирается в то, что писали.

    import-product-trace.py [--trace П] [--map П] [--archive П] [--lock П]
                            [--line lama] [--show]
    import-product-trace.py --selftest
Код возврата: 0 — записано или нечего добавлять, 1 — отказ.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import threading
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from config import из_файла, конфиг, корень_дерева, путь_harness_conf  # noqa: E402
from neighbor import модуль  # noqa: E402

exporter = модуль("export-map-to-product")

DEFAULT_LINE = "lama"
LINE_NAME = "Продукт: карта, перенесённая со старого харнеса"
LINE_SHORT = "Продукт (перенос)"
SECTION = re.compile(r"^## (.+?) — (\d+)\s*$")
CARD = re.compile(r"^### `(.+)`\s*$")
HOW_MARK = "*Чем проверить:* "
CHANNEL_REPLY = re.compile(r"^Владелец \d{4}-\d{2}-\d{2} в канал: «")
STALE_DETAILS = "Подробности: docs/dev-map-подробности.yaml"
HEADING_TO_STATUS = {heading: status for status, heading in exporter.STATUS_HEADINGS.items()}
NEEDS_REVIEW = "ждёт ревизии: из следа старого сервера разработки"
# test и done старого сервера — не «ждёт проверки» здесь; остальное — как было.
STATUS_HERE = {"test": "plan", "done": "plan"}
ARCHIVE_SKELETON = "# Архив карты разработки: закрытые задачи.\nversion: 1\nlines:\n"


# ── разбор следа ───────────────────────────────────────────────────────────

def parse_trace(text: str) -> tuple[list[dict], dict[str, int]]:
    """([карточка], {статус: число из заголовка раздела}).

    Число в заголовке сверяется с разобранным: разбор, потерявший карточку,
    обязан отказать, а не тихо привезти меньше."""
    cards, declared = [], {}
    status, card, body = None, None, []

    def close_card():
        if card is not None:
            card.update(split_body(body))
            cards.append(card)

    for line in text.splitlines():
        section = SECTION.match(line)
        if section or line.startswith("## "):
            close_card()
            card, body = None, []
            status = HEADING_TO_STATUS.get(section.group(1)) if section else None
            if status:
                declared[status] = int(section.group(2))
            continue
        found = CARD.match(line)
        if found and status:
            close_card()
            card, body = {"id": found.group(1), "status": status}, []
            continue
        if card is not None:
            body.append(line)
    close_card()

    for status, number in declared.items():
        got = sum(1 for c in cards if c["status"] == status)
        if status != "done" and got != number:
            raise ValueError(f"раздел «{exporter.STATUS_HEADINGS[status]}»: в заголовке "
                             f"{number}, разобрано {got} — разбор теряет карточки")
    return cards, declared


def split_body(lines: list[str]) -> dict:
    """Строки карточки после `### id` → title, summary, как_проверить."""
    lines = list(lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    title = ""
    if lines and lines[0].startswith("**") and lines[0].rstrip().endswith("**"):
        title = lines.pop(0).strip()[2:-2]
    text = "\n".join(lines).strip()
    how = ""
    if HOW_MARK in text:
        text, how = text.split(HOW_MARK, 1)
    summary = text.strip()
    fields = {"title": title, "summary": "" if summary == "—" else summary}
    if how.strip():
        fields["как_проверить"] = how.strip()
    return fields


# ── история следа: id закрытых ─────────────────────────────────────────────

def git(repo: Path, *args: str) -> str | None:
    answer = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True, check=False)
    return answer.stdout if answer.returncode == 0 else None


def closed_from_history(trace: Path, current_ids: set) -> tuple[list[dict], str]:
    """([карточка, пропавшая из следа], слово о том, как читалась история).

    Ревизии идут от новой к старой: первая встреча id — его последний текст."""
    top = git(trace.parent, "rev-parse", "--show-toplevel")
    if top is None:
        return [], "история не прочитана: след вне репозитория git"
    repo = Path(top.strip())
    relative = trace.resolve().relative_to(repo.resolve()).as_posix()
    revisions = git(repo, "log", "--format=%H", "--", relative)
    if revisions is None:
        return [], "история не прочитана: git log отказал"
    found: dict[str, dict] = {}
    for revision in revisions.split():
        text = git(repo, "show", f"{revision}:{relative}")
        if text is None:
            continue
        try:
            cards, _ = parse_trace(text)
        except ValueError:
            continue
        for card in cards:
            if card["id"] not in current_ids and card["id"] not in found:
                card["revision"] = revision[:7]
                found[card["id"]] = card
    return list(found.values()), f"ревизий следа прочитано {len(revisions.split())}"


# ── что куда ───────────────────────────────────────────────────────────────

def plan_import(cards: list[dict], closed: list[dict], source: str,
                today: str) -> tuple[list[dict], list[dict]]:
    """(карточки в карту, карточки в архив) — уже в форме карты."""
    to_map, to_archive = [], []
    for card in cards:
        entry = {"id": card["id"], "status": STATUS_HERE.get(card["status"], card["status"]),
                 "blocked": NEEDS_REVIEW, "статус_в_следе": card["status"], "источник": source,
                 "заведено": today, "title": card["title"], "summary": card["summary"]}
        if STALE_DETAILS in card["summary"]:
            entry["источник"] = (source + "; полный summary остался в подробностях "
                                 "старого харнеса, сюда не приехал")
        if card.get("как_проверить"):
            entry["как_проверить"] = card["как_проверить"]
        if CHANNEL_REPLY.match(card["summary"]):
            entry = {"id": entry["id"], "status": card["status"],
                     "closed_reason": "реплика канала старого сервера: карточку завёл "
                                      "map-from-message.py по сообщению, а не задача "
                                      "продукта; перенесена при импорте следа в архив",
                     "снято_когда": today, **{k: v for k, v in entry.items()
                                              if k not in ("id", "status", "blocked", "статус_в_следе")}}
            to_archive.append(entry)
            continue
        to_map.append(entry)
    for card in closed:
        entry = {"id": card["id"], "status": STATUS_HERE["done"], "blocked": NEEDS_REVIEW,
                 "статус_в_следе": "done",
                 "источник": (f"{source}; закрыта на старом сервере (в следе до "
                              f"ревизии {card['revision']}, затем только числом), "
                              f"подтверждение владельца сюда не доехало"),
                 "заведено": today, "title": card["title"], "summary": card["summary"]}
        if card.get("как_проверить"):
            entry["как_проверить"] = card["как_проверить"]
        to_map.append(entry)
    return to_map, to_archive


def card_text(entry: dict) -> str:
    """Карточка строками карты. Строки — JSON-кавычками: это валидный YAML в
    двойных кавычках и одна строка на поле (прополка читает summary строкой)."""
    lines = [f"      - id: {entry['id']}\n"]
    for key, value in entry.items():
        if key != "id":
            lines.append(f"        {key}: {json.dumps(value, ensure_ascii=False)}\n")
    return "".join(lines)


def line_header(line_id: str, with_board_fields: bool) -> str:
    head = f"\n  - id: {line_id}\n    layer: product\n    name: {json.dumps(LINE_NAME, ensure_ascii=False)}\n"
    if with_board_fields:
        head += f"    short: {json.dumps(LINE_SHORT, ensure_ascii=False)}\n    weight: 1\n"
    return head + "    tasks:\n"


def insert_into_line(text: str, line_id: str, block: str, header: str) -> str:
    """Текст файла с `block` в конце задач линии `line_id`; линии нет — она
    заводится последней в `lines:`. Ни одна прежняя строка не меняется."""
    rows = text.splitlines(keepends=True)
    if rows and not rows[-1].endswith("\n"):
        rows[-1] += "\n"
    start = next((i for i, r in enumerate(rows) if re.match(r"^lines:\s*$", r)), None)
    if start is None:
        raise ValueError("в файле нет раздела lines:")
    end = next((i for i in range(start + 1, len(rows)) if re.match(r"^\S", rows[i])), len(rows))
    own = next((i for i in range(start + 1, end)
                if re.match(rf"^\s{{0,4}}-\s+id:\s*{re.escape(line_id)}\s*$", rows[i])), None)
    if own is not None:
        end = next((i for i in range(own + 1, end)
                    if re.match(r"^\s{0,4}-\s+id:\s", rows[i])), end)
        header = ""
    while end > start + 1 and not rows[end - 1].strip():
        end -= 1
    rows[end:end] = [header + block]
    return "".join(rows)


def task_ids(data: dict) -> list[str]:
    return [t.get("id") for line in (data or {}).get("lines") or [] for t in line.get("tasks") or []]


def prepare(before_text: str, label: str, entries: list[dict], line_id: str, board: bool) -> str:
    """Новый текст файла с карточками, сверенный разбором; отказ — исключение.
    Пишет вызывающий — когда сверены ОБА файла, иначе архив уехал бы без карты."""
    before = task_ids(yaml.safe_load(before_text))
    after_text = insert_into_line(before_text, line_id, "".join(card_text(e) for e in entries),
                                  line_header(line_id, board))
    data = yaml.safe_load(after_text)
    after = task_ids(data)
    lost = set(before) - set(after)
    if lost:
        raise ValueError(f"{label}: правка потеряла задачи {sorted(lost)}")
    # Счёт по РАЗНЫМ id: дубль id прибавляет строку в список, но не задачу.
    if len(set(after)) != len(set(before)) + len(entries) or len(after) != len(before) + len(entries):
        raise ValueError(f"{label}: разных id было {len(set(before))}, стало {len(set(after))} "
                         f"(строк {len(after)}), добавляли {len(entries)}")
    written = {t["id"]: t for line in data["lines"] if line.get("id") == line_id
               for t in line.get("tasks") or []}
    for entry in entries:
        if written.get(entry["id"]) != entry:
            raise ValueError(f"{label}: карточка {entry['id']} разобралась не в то, что писали")
    return after_text


def replace_file(path: Path, text: str) -> None:
    """Полуфайла не бывает: временный файл рядом и атомарная замена."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as out:
        out.write(text)
    if path.exists():
        os.chmod(out.name, path.stat().st_mode & 0o7777)
    os.replace(out.name, path)


def run(trace: Path, map_path: Path, archive: Path, lock: Path, line_id: str,
        show: bool, today: str) -> dict:
    """Импорт целиком. Возвращает счёт для отчёта."""
    text = trace.read_text(encoding="utf-8")
    cards, declared = parse_trace(text)
    head = next((l for l in text.splitlines() if l.startswith("> Снято")), "")
    commit = re.search(r"коммит `([^`]+)`", head)
    source = (f"след карты старого харнеса ({trace.name}, коммит харнеса "
              f"{commit.group(1) if commit else 'не назван'})")
    closed, history = closed_from_history(trace, {c["id"] for c in cards})
    to_map, to_archive = plan_import(cards, closed, source, today)

    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        known = set(task_ids(yaml.safe_load(map_path.read_text(encoding="utf-8"))))
        if archive.exists():
            known |= set(task_ids(yaml.safe_load(archive.read_text(encoding="utf-8"))))
        skipped = sorted(e["id"] for e in to_map + to_archive if e["id"] in known)
        to_map = [e for e in to_map if e["id"] not in known]
        to_archive = [e for e in to_archive if e["id"] not in known]
        if not show:
            writes = []
            if to_map:
                writes.append((map_path, prepare(map_path.read_text(encoding="utf-8"), map_path.name,
                                                 to_map, line_id, board=True)))
            if to_archive:
                before = archive.read_text(encoding="utf-8") if archive.exists() else ARCHIVE_SKELETON
                writes.append((archive, prepare(before, archive.name, to_archive, line_id, board=False)))
            for path, new_text in writes:
                replace_file(path, new_text)
    by_status: dict[str, int] = {}
    for entry in to_map:
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
    return {"parsed": len(cards), "declared": declared, "closed": len(closed),
            "history": history, "to_map": len(to_map), "to_archive": len(to_archive),
            "by_status": by_status, "skipped": skipped}


def report(result: dict, show: bool) -> str:
    verb = "добавилось бы" if show else "добавлено"
    declared_done = result["declared"].get("done", 0)
    lines = [
        f"след: карточек с текстом {result['parsed']}, закрытых числом {declared_done}; "
        f"id закрытых из истории — {result['closed']} ({result['history']})",
        f"в карту {verb}: {result['to_map']} {dict(sorted(result['by_status'].items()))}",
        f"в архив (реплики канала) {verb}: {result['to_archive']}",
        f"уже были в карте или архиве — не тронуты: {len(result['skipped'])}",
    ]
    if declared_done > result["closed"]:
        lines.append(f"закрытых без id: {declared_done - result['closed']} — след их не называет, "
                     f"история тоже; потеряны до первого следа")
    return "\n".join(lines)


# ── самотест ───────────────────────────────────────────────────────────────

SELFTEST_MAP = """version: 1
queue: []
layers:
  - { id: product, label: "Продукт" }
lines:
  - id: product
    layer: product
    name: "Продукт"
    tasks:
      - id: pr.existing
        status: plan
        title: "Своя карточка смены"
        summary: "заголовок и статус не перетираются следом"
"""


MULTI_SUMMARY = 'Первый абзац с «ёлочками», "кавычками" и обратным слэшем C:\\путь.\n\nВторой абзац.'


def selftest_trace(cards_test: list[str], done: int) -> str:
    rows = ["# Карта разработки — след карты харнеса", "",
            "> Снято 13.10.2026 22:49 с карты харнеса, коммит `abc1234`.", "",
            "## Где остановлена работа", "", "текст", "",
            f"## Ждут проверки владельца или живой — {len(cards_test) + 1}", ""]
    for name in cards_test:
        rows += [f"### `{name}`", f"**Заголовок {name}**", "", f"Описание {name}. Две фразы.", ""]
        if name == "pr.a":
            rows += ["*Чем проверить:* открыть и увидеть", ""]
    rows += ["### `pr.reply`", "**доделать всё**", "",
             "Владелец 2026-10-12 в канал: «доделать всё». Карточка заведена КОДОМ.", ""]
    rows += ["## Запланировано — 2", "", "### `pr.existing`", "**Чужой заголовок из следа**", "",
             "Текст следа.", "", "### `pr.multi`", "**Многострочная**", "", MULTI_SUMMARY, "",
             f"## Закрыто — {done}", "", f"Закрытых карточек продукта: {done}.", ""]
    return "\n".join(rows)


def selftest() -> int:
    ok = True

    def check(why: str, good: bool, detail: str = ""):
        nonlocal ok
        ok &= good
        print(f"  {'ок   ' if good else 'ПЛОХО'} {why}{'' if good else ' — ' + detail}")

    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def tasks_of(path: Path, line_id: str) -> dict:
        data = yaml.safe_load(read(path)) or {}
        return {t["id"]: t for l in data.get("lines") or [] if l.get("id") == line_id
                for t in l.get("tasks") or []}

    with tempfile.TemporaryDirectory() as tmp:
        try:
            paths(Path(tmp), check, read, tasks_of)
        except (OSError, ValueError, KeyError, StopIteration, yaml.YAMLError) as error:
            check("самотест дошёл до конца", False, f"оборвался: {error!r}")

    print(f"САМОТЕСТ {'ПРОЙДЕН' if ok else 'ПРОВАЛЕН'}: 13 путей, шесть больных случаев")
    return 0 if ok else 1


def paths(root: Path, check, read, tasks_of) -> None:
    """Пути самотеста; оборванный путь — строка ПЛОХО, а не traceback."""
    repo = root / "product"
    (repo / "docs").mkdir(parents=True)
    trace = repo / "docs" / "КАРТА-РАЗРАБОТКИ.md"
    env = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    git(repo, "init", "-q")
    trace.write_text(selftest_trace(["pr.a", "pr.gone"], 0), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *env, "commit", "-qm", "1")
    trace.write_text(selftest_trace(["pr.a"], 1), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *env, "commit", "-qm", "2")
    map_path, archive, lock = root / "dev-map.yaml", root / "archive.yaml", root / "dev-map.lock"
    map_path.write_text(SELFTEST_MAP, encoding="utf-8")

    try:
        parse_trace(selftest_trace(["pr.a"], 0).replace("— 2\n", "— 3\n", 1))
        check("БОЛЬНОЙ СЛУЧАЙ: заголовок раздела обещает больше, чем разобрано — отказ", False, "прошло")
    except ValueError:
        check("БОЛЬНОЙ СЛУЧАЙ: заголовок раздела обещает больше, чем разобрано — отказ", True)

    shown = run(trace, map_path, archive, lock, "lama", True, "2026-10-14")
    check("--show ничего не пишет", read(map_path) == SELFTEST_MAP
          and not archive.exists() and shown["to_map"] == 3, str(shown))

    first = run(trace, map_path, archive, lock, "lama", False, "2026-10-14")
    lama = tasks_of(map_path, "lama")
    data = yaml.safe_load(read(map_path))
    card = lama.get("pr.a", {})
    check("карточка следа в линии lama слоя product с «чем проверить»",
          card.get("как_проверить") == "открыть и увидеть"
          and any(l["id"] == "lama" and l["layer"] == "product" for l in data["lines"]), str(lama))
    check("БОЛЬНОЙ СЛУЧАЙ (указание о ревизии): test следа — plan + blocked «ждёт ревизии», "
          "прежний статус в статус_в_следе",
          card.get("status") == "plan" and card.get("blocked") == NEEDS_REVIEW
          and card.get("статус_в_следе") == "test", str(card))
    check("каждая импортированная карточка несёт blocked, своя карточка смены — нет",
          lama and all(t.get("blocked") == NEEDS_REVIEW for t in lama.values())
          and "blocked" not in tasks_of(map_path, "product").get("pr.existing", {}), str(lama))
    gone = lama.get("pr.gone", {})
    check("закрытая из истории — plan + blocked, статус_в_следе done, «подтверждение не доехало»",
          gone.get("status") == "plan" and gone.get("статус_в_следе") == "done"
          and "не доехало" in gone.get("источник", ""), str(gone))
    check("многострочный summary с кавычками и обратным слэшем — дословно",
          lama.get("pr.multi", {}).get("summary") == MULTI_SUMMARY, str(lama.get("pr.multi")))
    archived = list(tasks_of(archive, "lama"))
    check("реплика канала — в архив, не на доску",
          archived == ["pr.reply"] and "pr.reply" not in lama, f"архив {archived}")
    existing = tasks_of(map_path, "product").get("pr.existing", {})
    check("БОЛЬНОЙ СЛУЧАЙ: карточка с уже существующим id не перетирается",
          existing.get("title") == "Своя карточка смены" and existing.get("status") == "plan"
          and "pr.existing" in first["skipped"], str(existing))

    frozen = (read(map_path), read(archive))
    second = run(trace, map_path, archive, lock, "lama", False, "2026-10-15")
    check("БОЛЬНОЙ СЛУЧАЙ: повтор не дублирует — карта и архив байт в байт",
          frozen == (read(map_path), read(archive))
          and second["to_map"] == 0 and second["to_archive"] == 0, str(second))

    duplicate = {"id": "pr.a", "status": "plan", "title": "дубль", "summary": "дубль"}
    try:
        prepare(read(map_path), "карта", [duplicate], "lama", True)
        check("БОЛЬНОЙ СЛУЧАЙ: вставка дубля id — сверка «было + добавляли = стало» отказывает",
              False, "дубль прошёл")
    except ValueError:
        check("БОЛЬНОЙ СЛУЧАЙ: вставка дубля id — сверка «было + добавляли = стало» отказывает", True)

    trace.write_text(selftest_trace(["pr.a", "pr.new"], 1), encoding="utf-8")
    before = read(map_path)
    with open(lock, "a", encoding="utf-8") as other_writer:
        fcntl.flock(other_writer, fcntl.LOCK_EX)
        worker = threading.Thread(target=run, args=(trace, map_path, archive, lock, "lama",
                                                    False, "2026-10-15"))
        worker.start()
        worker.join(0.5)
        waited = worker.is_alive() and read(map_path) == before
    worker.join(10)
    check("БОЛЬНОЙ СЛУЧАЙ: другой писатель держит dev-map.lock — импорт ждёт и пишет после",
          waited and "pr.new" in tasks_of(map_path, "lama"),
          "записал, не дождавшись лока" if not waited else "после лока не записал")
    data = yaml.safe_load(read(map_path))
    lines = [l["id"] for l in data["lines"]]
    check("новая карточка следа ложится в ту же линию, вторая линия не заводится",
          lines == ["product", "lama"] and list(tasks_of(map_path, "lama"))[-1] == "pr.new", str(lines))


# ── главный ход ────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Импорт следа карты продукта в dev-map.yaml")
    parser.add_argument("--trace")
    parser.add_argument("--map")
    parser.add_argument("--archive")
    parser.add_argument("--lock")
    parser.add_argument("--line", default=DEFAULT_LINE)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()

    settings = конфиг()
    trace = Path(args.trace) if args.trace else exporter.trace_path(из_файла(путь_harness_conf()))
    if trace is None:
        print("в harness.conf пуст PRODUCT_REPO — где след, неизвестно; "
              "задайте ключ или позовите с --trace <путь>", file=sys.stderr)
        return 1
    map_path = Path(args.map or Path(settings.get("PROJECT_DIR") or корень_дерева(__file__)) / "dev-map.yaml")
    archive = Path(args.archive or map_path.parent / "docs" / "dev-map-archive.yaml")
    lock = Path(args.lock or Path(settings["LOG_DIR"]) / "locks"
                / (settings.get("SERVICE_LOCKS_SUBDIR") or "services") / "dev-map.lock")
    try:
        result = run(trace, map_path, archive, lock, args.line, args.show,
                     datetime.date.today().isoformat())
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"[импорт следа] ОТКАЗ, ничего не записано: {error}", file=sys.stderr)
        return 1
    print(report(result, args.show))
    return 0


if __name__ == "__main__":
    sys.exit(main())
