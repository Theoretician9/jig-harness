#!/usr/bin/env python3
"""Снимок доски канбана для панели владельца — функциями гейта пайплайна.

    python3 scripts/build-board.py      # пишет board.json в каталог состояния панели

Стадию задачи нигде не хранят: её вычисляет гейт `check-pipeline.py` из статуса
и бумаг. Доска зовёт ЕГО функции (импортом, не копией), поэтому то, что
владелец видит в колонке, — ровно то, что гейт скажет при закрытии задачи.
Служба панели git не зовёт и в проект не пишет — снимок собирает эта сторона
(пользователь agent) и кладёт готовым в каталог состояния панели.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config  # noqa: E402

HERE = Path(__file__).resolve().parent
TEXT_LIMIT = 4000
SKILLS_TAIL = 2000
COMMITS_LIMIT = 20

QUEUE, CHECK, DONE = "Очередь", "Проверка", "Готово"


def load_script(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_gate():
    return load_script("check_pipeline", "check-pipeline.py")


@functools.lru_cache(maxsize=1)
def key_filter():
    # Тот же фильтр, что у карточки: ключ, присланный владельцем в канал или
    # попавший в журнал задачи, не едет ни в карту, ни на доску веб-панели.
    return load_script("map_from_message", "map-from-message.py").без_ключей


def column_name(step: str) -> str:
    return step.replace("_", " ")


def step_list(card: dict, judge, root, settings) -> list:
    """Все шаги «крупной» с отметкой: бумага есть / нет / шаг машиной не судится."""
    required = judge.шаги_для_задачи(settings, "крупная", card)
    steps = []
    for step in judge.шаги_уровня(settings, "крупная"):
        path, _name = judge._где_артефакт(step, card.get("id") or "", card.get("spec"), root)
        if step not in required:
            steps.append({"шаг": step, "путь": None, "есть": None, "почему": "не положен этой задаче"})
        elif path is None:
            steps.append({"шаг": step, "путь": None, "есть": None, "почему": "машиной не судится"})
        else:
            debts = judge.беды_в_двух_корнях(card, [step], root)
            steps.append({"шаг": step, "путь": str(path), "есть": not debts,
                          "почему": "; ".join(debts)})
    return steps


def first_missing(steps: list) -> str | None:
    for step in steps:
        if step["есть"] is False:
            return column_name(step["шаг"])
    return None


def approval_pill(card: dict, judge) -> str:
    sent = judge.spec_approval.отправлена(card.get("id") or "")
    when = str((sent or {}).get("ts") or "")[:10]
    return f"ждёт твоего слова по спеке, отправлена {when}" if sent else \
        "ждёт твоего слова по спеке, не отправлена"


def wip_column(card: dict, judge) -> tuple:
    """(колонка, пилюли, шаги) задачи в работе — по суждению гейта."""
    price = judge._модуль_цены()
    settings = price.конфиг()
    first = column_name(judge.шаги_уровня(settings, "крупная")[0])
    if not card.get("глубина"):
        return first, ["глубины нет — шаги не назначены"], []
    root, trouble = judge.корень_бумаг(card)
    if trouble:
        reason = "PRODUCT_REPO пуст" if "пуст" in trouble else trouble
        return first, [f"судить нечем: {reason}"], []
    value, debts = judge.долг_задачи(card, price, settings)
    steps = step_list(card, judge, root, settings)
    if value is None:
        return first, ["история не прочитана: git молчит — это не «чисто»"], steps
    level = price.уровень(value, settings["пороги"])
    if level != "крупная":
        return (first_missing(steps) or column_name("ревью_кода"),
                [f"гейт шагов не требует: цена {value}, по коммитам {level}"], steps)
    waiting = judge.spec_approval.беды_одобрения(card.get("id") or "", card, root)
    if any(d in waiting for d in debts):
        return column_name("ревью_спеки"), [approval_pill(card, judge)], steps
    missing = first_missing(steps)
    if missing:
        return missing, [], steps
    return column_name("ревью_кода"), ["бумаги собраны — закрыть в test"], steps


def column(card: dict, judge) -> tuple:
    """(колонка, пилюли, шаги) карточки любого статуса."""
    status = card.get("status")
    if status == "wip":
        place, pills, steps = wip_column(card, judge)
    elif status == "plan":
        place, pills, steps = QUEUE, [], []
    elif status == "беклог":
        place, pills, steps = QUEUE, ["не трогаю без слова"], []
    elif status == "test":
        place, steps = CHECK, []
        pills = [card["чем_подтверждено"]] if card.get("чем_подтверждено") else ["нечем подтверждено"]
    elif status == "принято":
        place, pills, steps = DONE, ["проверено машиной"], []
    elif status == "done":
        place, pills, steps = DONE, ["проверено вами"], []
    else:
        place, pills, steps = QUEUE, [f"непонятный статус: {status}"], []
    if card.get("blocked"):
        pills = pills + [f"ждёт: {card['blocked']}"]
    return place, pills, steps


def read_text(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return key_filter()(text)[:TEXT_LIMIT]


def sources(card: dict, log_dir: Path) -> list:
    """Слова владельца, из которых задача заведена: inbox — текстом, иное — строкой."""
    found = []
    for raw in [card.get("источник")] + list(card.get("также_из") or []):
        if not raw:
            continue
        raw = str(raw)
        text = None
        if raw.startswith("inbox:"):
            message = raw[len("inbox:"):]
            for folder in (log_dir / "inbox" / "обработано", log_dir / "inbox"):
                text = read_text(folder / f"{message}.txt")
                if text is not None:
                    break
        found.append({"id": raw, "текст": text, "строка": raw})
    return found


def skills(task_id: str, log_dir: Path) -> list:
    try:
        lines = (log_dir / "skills.jsonl").read_text(encoding="utf-8").splitlines()[-SKILLS_TAIL:]
    except OSError:
        return []
    calls = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("task") == task_id:
            calls.append({"ts": record.get("ts"), "скил": record.get("skill"),
                          "откуда_задача": record.get("откуда_задача")})
    return calls


def commits(task_id: str, repo: Path) -> list:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "log", f"-n{COMMITS_LIMIT}", "--format=%h%x09%ad%x09%s",
             "--date=short", f"--grep=Dev-Map: {task_id}$"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    return [dict(zip(("хеш", "дата", "тема"), line.split("\t", 2)))
            for line in done.stdout.splitlines() if line.count("\t") == 2]


def archived(map_path: Path) -> int:
    import yaml
    try:
        data = yaml.safe_load((map_path.parent / "docs" / "dev-map-archive.yaml")
                              .read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return 0
    return sum(len(line.get("tasks") or []) for line in data.get("lines") or [])


def snapshot_of(card: dict, judge, repo: Path, log_dir: Path) -> dict:
    place, pills, steps = column(card, judge)
    task_id = card.get("id") or ""
    value, level = None, None
    if card.get("status") == "wip" and card.get("глубина"):
        price = judge._модуль_цены()
        settings = price.конфиг()
        features = price.признаки_задачи(task_id)
        if features is not None:
            value = price.цена(features, settings["веса"])
            level = price.уровень(value, settings["пороги"])
    return {
        "id": task_id, "название": card.get("title") or task_id, "статус": card.get("status"),
        "колонка": place, "пилюли": pills, "уровень_объявлен": (card.get("глубина") or {}).get("уровень"),
        "уровень_по_коммитам": level, "цена": value, "шаги": steps,
        "источники": sources(card, log_dir), "журнал": read_text(repo / ".tasks" / task_id / "журнал.md"),
        "коммиты": commits(task_id, repo), "скилы": skills(task_id, log_dir),
    }


def build(map_path, judge, now: datetime) -> dict:
    map_path = Path(map_path)
    price = judge._модуль_цены()
    columns = [QUEUE] + [column_name(s) for s in judge.шаги_уровня(price.конфиг(), "крупная")] \
        + [CHECK, DONE]
    board = {"снято": now.isoformat(timespec="seconds"), "направление": config.активная_линия(),
             "архив": archived(map_path), "колонки": columns, "задачи": [], "беда": None}
    import yaml
    try:
        data = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as trouble:
        board["беда"] = f"карта не прочитана: {trouble}"
        return board
    log_dir = Path(config.log_dir())
    # Направление, над которым работаем; пусто — все. Отложенные направления
    # на доске не мешают — так же делит карту страница панели.
    line = board["направление"]
    own = {t.get("id") for l in data.get("lines") or [] if l.get("id") == line
           for t in l.get("tasks") or []}
    cards = [c for c in judge._все_карточки() if not line or c.get("id") in own]
    board["задачи"] = [snapshot_of(c, judge, map_path.parent, log_dir) for c in cards]
    return board


def write_atomic(target: Path, board: dict) -> None:
    # Каталог пишет и служба панели: временное имя — уникальное (O_EXCL, без
    # перехода по симлинку), права — по дескриптору, а не по пути.
    descriptor, temp = tempfile.mkstemp(dir=target.parent, prefix=".board.")
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(board, handle, ensure_ascii=False, indent=1)
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def main() -> int:
    judge = load_gate()
    now = datetime.now()
    board = build(judge.КОРЕНЬ / "dev-map.yaml", judge, now)
    write_atomic(Path(config.panel_state_dir()) / "board.json", board)
    # Метка пульса: молчащий снимок — устаревшая доска, и сторож меток обязан
    # назвать это владельцу, а не ждать, пока он сам заметит старое время.
    marks = Path(config.heartbeat_dir())
    marks.mkdir(parents=True, exist_ok=True)
    (marks / "build-board").touch()
    print(f"доска: задач {len(board.get('задачи') or [])}, снято {now:%H:%M}"
          + (f" — беда: {board['беда']}" if board.get("беда") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
