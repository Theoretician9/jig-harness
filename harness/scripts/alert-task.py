#!/usr/bin/env python3
"""Писатель задач по тревогам сторожей: весть владельцу становится карточкой.

Владелец 25.10.2026: сторож шлёт весть только ему, а работу должна делать
модель. Здесь весть сторожа (ключ `<сторож>:<род беды>`) превращается в
карточку карты, которую модель берёт сама. Спека: .tasks/hr.watchers-report-
must-create-model-task/spec.md (ред. 2), состояния зова — §3.2.

    alert-task.py --key K --title T --text X [--watcher W] [--line L]
    alert-task.py --resolve K        # сторож видит зелёное
    alert-task.py --drain            # донести отложенные намерения
    alert-task.py --is-green K       # приёмка: 0 и «ЗЕЛЁНОЕ K», иначе 1
    alert-task.py --open             # открытые ключи
    alert-task.py --selftest
Коды: 0 заведена/повтор/зелёное/беклог/донос · 1 отказ (карта нечитаема) ·
2 кривой вызов · 4 отложено (лок занят) · 5 потолок сторожа · 6 зов из сессии.

Состояние тревоги живёт в журнале $LOG_DIR/тревоги-сторожей.jsonl, а не в
карте: карта пишется один раз, при рождении карточки, повтор — строка журнала.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# Дверь для мутаций: проба подставляет копию библиотеки карточек.
sys.path.insert(0, os.environ.get("HARNESS_CARDS_LIB") or str(HERE / "lib"))
import cards  # noqa: E402
import patterns  # noqa: E402
from config import активная_линия, из_файла, конфиг, путь_harness_conf  # noqa: E402
from neighbor import модуль  # noqa: E402

# Форму ключа судит patterns.py (один судья); запасная нужна, пока форма не
# внесена туда, и совпадает с ней по смыслу.
KEY_FORM = getattr(patterns, "КЛЮЧ_ТРЕВОГИ", None) or re.compile(
    r"[a-z0-9][a-z0-9._-]*:[a-z0-9:._-]*")
KEY_MAX = 80
SESSION_REFUSED, BAD_CALL, DEFERRED, CEILING, REFUSED = 6, 2, 4, 5, 1
JOURNAL_ROW_MAX = 4096


def setting(name: str, default: str) -> str:
    """Окружение старше файла: проба задаёт пороги своим стендом."""
    return os.environ.get(name) or из_файла(путь_harness_conf()).get(name) or default


class Paths:
    def __init__(self) -> None:
        passport = конфиг()
        self.log = Path(passport["LOG_DIR"])
        project = Path(passport.get("PROJECT_DIR") or HERE.parent)
        self.map = Path(os.environ.get("HARNESS_DEVMAP") or project / "dev-map.yaml")
        self.archive = Path(os.environ.get("DEVMAP_ARCHIVE")
                            or project / "docs" / "dev-map-archive.yaml")
        self.project = project
        self.journal = self.log / cards.JOURNAL_NAME
        self.queue = self.log / cards.QUEUE_NAME
        self.queue_lock = self.log / "locks" / "тревоги-ждут.lock"
        self.journal_lock = self.log / "locks" / "тревоги-журнал.lock"
        self.failures = self.log / "tg_failures.log"


def claude_ancestors() -> list:
    """Предки процесса с именем `claude`: окружение снимается одной командой
    (`env -u`), а родословную процесс сменить не может. Отрыв через `setsid`
    делает сироту — его ловит правило guard_bash по тексту команды."""
    found, pid = [], os.getppid()
    while pid > 1:
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            break
        fields = dict(line.split(":\t", 1) for line in status.splitlines() if ":\t" in line)
        if fields.get("Name", "").strip() == "claude":
            found.append(pid)
        pid = int(fields.get("PPid", "0").strip() or 0)
    return found


def called_from_session() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_SESSION_ID")) or bool(claude_ancestors())


def refuse_session(paths: Paths, key: str, watcher: str | None) -> int:
    # Тревогу заводит и снимает демон: карточка или «зелёное» от смены накрутило
    # бы себе вес либо закрыло приёмку без починки.
    log_event(paths, "из-сессии", key, why="зов из сессии", watcher=watcher)
    print("alert-task: зов из сессии отказан — тревогой ведает сторож", file=sys.stderr)
    return SESSION_REFUSED


def now_stamp() -> str:
    # Микросекунды: «зелёное» и намерение отличаются порой долями секунды, а
    # снятие отложенного (Р-23) сравнивает их по времени.
    return datetime.datetime.now().astimezone().isoformat(timespec="microseconds")


def watcher_of(key: str, given: str | None = None) -> str:
    return given or key.split(":")[0]


def journal_lines(paths: Paths) -> list:
    try:
        return paths.journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def log_event(paths: Paths, event: str, key: str, card_id: str = "", why: str = "",
              watcher: str | None = None) -> None:
    row = {"ts": now_stamp(), "ключ": key, "событие": event, "id": card_id,
           "сторож": watcher_of(key, watcher), "почему": why[:500]}
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) >= JOURNAL_ROW_MAX:
        raise ValueError("строка журнала длиннее 4 КБ")
    paths.log.mkdir(parents=True, exist_ok=True)
    paths.journal_lock.parent.mkdir(parents=True, exist_ok=True)
    # Общий замок: сжатие журнала подменяет файл, и строка, дописанная в старый,
    # пропала бы. Между собой писатели не мешают — строка короче страницы под
    # O_APPEND пишется целиком.
    with open(paths.journal_lock, "a", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_SH)
        fd = os.open(paths.journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def compact_journal(paths: Paths) -> None:
    """Сжатие журнала, когда он вырос: повторы старше суток сливаются счётчиком.
    Без этого каждый прогон каждого сторожа растит файл, который читает осмотр
    очереди. Занятый замок — не беда: сожмёт следующий зов."""
    limit = int(setting("ALERT_JOURNAL_COMPACT_BYTES", "262144"))
    try:
        if paths.journal.stat().st_size < limit:
            return
    except OSError:
        return
    paths.journal_lock.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.journal_lock, "a", encoding="utf-8") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        rows = cards.compact_rows(cards.parse_journal(journal_lines(paths)), time.time())
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        tmp = paths.journal.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, paths.journal)


def valid_key(key: str) -> bool:
    return len(key) <= KEY_MAX and ":" in key and bool(KEY_FORM.fullmatch(key))


def archive_ids(paths: Paths) -> set:
    try:
        return cards.task_ids(yaml.safe_load(paths.archive.read_text(encoding="utf-8")) or {})
    except OSError:
        return set()


# ── карточка ────────────────────────────────────────────────────────────────

def recheck_command(paths: Paths, watcher: str) -> str:
    demon = paths.project / "harness" / "demons" / f"{watcher}.sh"
    if demon.is_file():
        return f"bash harness/demons/{watcher}.sh"
    return f"дождаться следующего прогона сторожа {watcher}"


def build_summary(paths: Paths, watcher: str, text: str) -> str:
    """Не длиннее потолка прополки. В хвосте НАРОЧНО стоит «починить»: только по
    этому слову судья характера даёт `починка`, а не полный пайплайн."""
    ceiling = int(setting("DEVMAP_SUMMARY_MAX", "500"))
    head = f"Сторож {watcher} {datetime.datetime.now():%d.%m %H:%M}: "
    tail = f" Разобрать причину и починить. Перепроверить сторожем: {recheck_command(paths, watcher)}."
    if модуль("limit_mark").лимит_держится():
        tail += " Лимит подписки: держится."
    body = cards.без_ключей(text).strip().replace("\n", " ")
    room = max(0, ceiling - len(head) - len(tail))
    if len(body) > room:
        body = body[:max(0, room - 1)] + "…"
    return (head + body + tail)[:ceiling]


def render_card(card_id: str, key: str, title: str, summary: str, watcher: str) -> str:
    today = datetime.date.today().isoformat()
    check = {"команда": f"python3 scripts/alert-task.py --is-green {key}",
             "ждём": f"ЗЕЛЁНОЕ {key}"}
    depth = cards.глубина_строкой(f"{title} {summary}", today)
    return "\n".join([
        f"      - id: {card_id}",
        "        status: plan",
        "        owner_testable: false",
        f"        источник: {cards.ALERT_SOURCE_PREFIX}{key}",
        f"        заведено: \"{today}\"",
        "        завёл: scripts/alert-task.py",
        f"        глубина: {depth}",
        f"        title: {json.dumps(title, ensure_ascii=False)}",
        f"        summary: {json.dumps(summary, ensure_ascii=False)}",
        f"        проверка: {{ команда: {json.dumps(check['команда'], ensure_ascii=False)}, "
        f"ждём: {json.dumps(check['ждём'], ensure_ascii=False)} }}",
        "        volume: { loc: 30, complexity: 2, tokens_k: 30, estimated: true }",
        "        when: { started: null, done: null, tested: null, estimated: true }",
        "",
    ])


# ── зов: решение под локом ──────────────────────────────────────────────────

def open_keys_of(paths: Paths, map_text: str, watcher: str, skip: str) -> list:
    """Открытые ключи сторожа: журнал говорит «открыта» И карточка ещё открыта.
    Только журнал держал бы место за карточкой, которую уже закрыли руками."""
    rows = cards.parse_journal(journal_lines(paths))
    folded = cards.fold_journal(rows)
    keys = {r["ключ"] for r in rows
            if r.get("сторож") == watcher and r.get("ключ") and r["ключ"] != skip}
    return sorted(k for k in keys if cards.state_of(folded.get(k)) == "открыта"
                  and cards.open_card_by_source(map_text, cards.ALERT_SOURCE_PREFIX + k))


class Outcome:
    def __init__(self) -> None:
        self.kind = ""
        self.card_id = ""
        self.notice = ""
        self.open_count = 0
        self.refused_count = 0
        self.watcher = ""


def process(paths: Paths, intent: dict, wait: float, born_event: str) -> Outcome:
    """Один путь пп.2–5 §3.2 — и для живого зова, и для донесения очереди."""
    key, line_id = intent["key"], intent.get("line") or "harness"
    watcher = watcher_of(key, intent.get("watcher"))
    source = cards.ALERT_SOURCE_PREFIX + key
    result = Outcome()
    result.watcher = watcher
    chosen: dict = {}

    def decide(map_text: str) -> bool:
        data = yaml.safe_load(map_text) or {}
        found = cards.open_card_by_source(map_text, source)
        if found:
            result.kind = "беклог" if found.get("status") == "беклог" else "повтор"
            result.card_id = found["id"]
            log_event(paths, result.kind, key, found["id"], watcher=watcher)
            return False
        opened = open_keys_of(paths, map_text, watcher, key)
        if len(opened) >= int(setting("ALERT_TASKS_PER_WATCHER", "3")):
            result.kind = "потолок"
            result.open_count = len(opened)
            refused = {r["ключ"] for r in cards.parse_journal(journal_lines(paths))
                       if r.get("событие") == "потолок" and r.get("сторож") == watcher}
            result.refused_count = len(refused | {key})
            log_event(paths, "потолок", key, why=f"открыто {len(opened)}", watcher=watcher)
            return False
        line = next((x for x in data.get("lines") or [] if x.get("id") == line_id), None)
        if line is None:
            raise cards.MapRefused(f"ОТКАЗ: в карте нет линии «{line_id}»")
        chosen["line"] = line
        return True

    def build(map_text: str) -> list:
        data = yaml.safe_load(map_text) or {}
        taken = cards.task_ids(data) | archive_ids(paths)
        title = cards.без_ключей(intent.get("title") or key).strip()[:110] or key
        summary = build_summary(paths, watcher, intent.get("text") or title)
        prefix = cards.приставка_линии(chosen["line"])
        chosen["id"] = cards.идентификатор(prefix, title, taken, "alert-" + key.replace(":", "-"))
        chosen["title"], chosen["summary"] = title, summary
        return [render_card(chosen["id"], key, title, summary, watcher)]

    def committed() -> None:
        active = активная_линия()
        why = ""
        if active and active != line_id:
            why = f"линия {line_id} не активна — будильник её не назовёт"
            result.notice = why
        log_event(paths, born_event, key, chosen["id"], why, watcher=watcher)
        result.kind, result.card_id = born_event, chosen["id"]

    cards.write_cards_locked(paths.map, line_id, build, recheck=decide,
                             lock_wait_sec=wait, committed=committed)
    return result


# ── очередь отложенных намерений ───────────────────────────────────────────

class QueueLock:
    """Короткий замок очереди: ничей другой, кроме писателей очереди."""

    def __init__(self, paths: Paths) -> None:
        paths.queue_lock.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(paths.queue_lock, "a", encoding="utf-8")

    def __enter__(self):
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc) -> None:
        self.handle.close()


def queue_rows(paths: Paths) -> list:
    try:
        lines = paths.queue.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [r for r in cards.parse_journal(lines) if isinstance(r.get("key"), str) and r["key"]]


def store_queue(paths: Paths, rows: list) -> None:
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    tmp = paths.queue.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, paths.queue)


def enqueue(paths: Paths, intent: dict) -> None:
    """Намерение по ключу одно: повторный зов при той же беде обновляет строку,
    а не растит очередь на каждом прогоне сторожа."""
    with QueueLock(paths):
        rows = [r for r in queue_rows(paths) if r.get("key") != intent["key"]]
        store_queue(paths, rows + [intent])


def green_after(lines: list, key: str, stamp: str) -> bool:
    return any(r.get("ключ") == key and r.get("событие") == cards.GREEN_EVENT
               and cards.epoch(r.get("ts")) > cards.epoch(stamp) for r in cards.parse_journal(lines))


def drain(paths: Paths) -> dict:
    stats = {"added": 0, "repeats": 0, "dropped": 0, "ceiling": 0, "left": 0, "reason": ""}
    if not paths.queue.exists() or paths.queue.stat().st_size == 0:
        return stats
    wait = float(setting("ALERT_LOCK_WAIT_SEC", "5"))
    with QueueLock(paths):
        rows = queue_rows(paths)
        broken = len([x for x in paths.queue.read_text(encoding="utf-8").splitlines()
                      if x.strip()]) - len(rows)
        if broken:
            print(f"alert-task: в очереди {broken} строк без ключа пропущено", file=sys.stderr)
        remaining, stopped = [], False
        for row in rows:
            if stopped:
                remaining.append(row)
                continue
            key = row["key"]
            if green_after(journal_lines(paths), key, str(row.get("ts"))):
                log_event(paths, "снята-до-записи", key, watcher=row.get("watcher"))
                stats["dropped"] += 1
                continue
            try:
                outcome = process(paths, row, wait, "донесена")
            except cards.LockBusy:
                stopped, stats["reason"] = True, "лок карты занят"
                remaining.append(row)
                continue
            except (yaml.YAMLError, OSError, cards.MapRefused) as error:
                stopped, stats["reason"] = True, f"карта не принимает: {error}"
                remaining.append(row)
                continue
            if outcome.kind == "донесена":
                stats["added"] += 1
            elif outcome.kind == "повтор":
                stats["repeats"] += 1
            elif outcome.kind == "потолок":
                stats["ceiling"] += 1
                say_ceiling(outcome)
            else:
                stats["dropped"] += 1
        store_queue(paths, remaining)
        stats["left"] = len(remaining)
    return stats


# ── сообщения наружу ───────────────────────────────────────────────────────

def board_say(key: str, text: str, minutes: int) -> None:
    done = subprocess.run([sys.executable, str(HERE / "board.py"), "--сказать", key, text,
                           "--на", str(minutes)], capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        print(f"alert-task: доска не приняла «{key}»: {done.stderr.strip()[:150]}", file=sys.stderr)


def say_ceiling(outcome: Outcome) -> None:
    """Одна доска на оба пути: отложенная беда, упёршаяся в потолок при донесении,
    не должна исчезать тише, чем живой зов."""
    board_say("тревоги-выше-потолка",
              f"{outcome.watcher}: открыто {outcome.open_count}, отказано {outcome.refused_count}",
              1440)


def note_failure(paths: Paths, key: str, why: str) -> None:
    row = {"ts": now_stamp(), "script": "alert-task", "why": why[:300], "key": key}
    with open(paths.failures, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── команды ────────────────────────────────────────────────────────────────

def command_raise(paths: Paths, args) -> int:
    key = args.key
    if called_from_session():
        return refuse_session(paths, key, args.watcher)
    intent = {"ts": now_stamp(), "key": key, "title": args.title,
              "text": args.text if args.text is not None else args.title,
              "line": args.line or "harness", "watcher": watcher_of(key, args.watcher)}
    try:
        compact_journal(paths)
        drain(paths)
    except (yaml.YAMLError, OSError, cards.MapRefused):
        pass          # причину назовёт сам зов ниже, тем же отказом
    try:
        outcome = process(paths, intent, float(setting("ALERT_LOCK_WAIT_SEC", "5")), "заведена")
    except cards.LockBusy:
        enqueue(paths, intent)
        log_event(paths, "отложено", key, why="лок карты занят", watcher=intent["watcher"])
        print(f"отложено {key}")
        return DEFERRED
    except (yaml.YAMLError, OSError, cards.MapRefused) as error:
        enqueue(paths, intent)
        why = f"карта не принимает: {error}"
        log_event(paths, "отказ", key, why=why, watcher=intent["watcher"])
        note_failure(paths, key, why)
        board_say("тревога-не-заведена", f"{key}: {why}"[:200], 180)
        print(f"alert-task: {why}", file=sys.stderr)
        return REFUSED
    if outcome.kind == "потолок":
        say_ceiling(outcome)
        print(f"потолок {outcome.watcher}: открыто {outcome.open_count}")
        return CEILING
    print(f"{outcome.kind} {outcome.card_id}")
    if outcome.notice:
        print(outcome.notice)
    return 0


def command_resolve(paths: Paths, key: str) -> int:
    if called_from_session():
        return refuse_session(paths, key, None)
    queued = any(r.get("key") == key for r in queue_rows(paths))
    if cards.key_state(journal_lines(paths), key) == "открыта" or queued:
        # Отложенное намерение тоже закрывается: иначе донос родил бы карточку
        # на беду, которой уже нет (Р-23).
        log_event(paths, cards.GREEN_EVENT, key)
    return 0


def command_is_green(paths: Paths, key: str) -> int:
    lines = journal_lines(paths)
    state = cards.key_state(lines, key)
    if state == "зелёное":
        print(f"ЗЕЛЁНОЕ {key}")
        return 0
    if state == "нет":
        print(f"НЕТ {key}")
        return 1
    last = [r for r in cards.parse_journal(lines) if r.get("ключ") == key][-1]
    print(f"КРАСНОЕ {key}: {last.get('событие')}, {last.get('ts')}")
    return 1


def command_open(paths: Paths) -> int:
    rows = cards.parse_journal(journal_lines(paths))
    for key, item in sorted(cards.fold_journal(rows).items()):
        if cards.state_of(item) == "открыта":
            birth = item["birth"]
            print(f"{key}\t{birth['id']}\tповторов {item['repeats']}\tс {birth.get('ts')}")
    return 0


def command_drain(paths: Paths) -> int:
    compact_journal(paths)
    stats = drain(paths)
    ceiling = f", потолок {stats['ceiling']}" if stats["ceiling"] else ""
    print(f"донос: донесено {stats['added']}, повторов {stats['repeats']}, "
          f"снято {stats['dropped']}, осталось {stats['left']}{ceiling}")
    if stats["left"]:
        print(f"в очереди {stats['left']}: {stats['reason']}")
    return 0


def run_selftest() -> int:
    probe = HERE / "test_alert_task.py"
    return subprocess.run([sys.executable, str(probe)]).returncode


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--key")
    parser.add_argument("--title")
    parser.add_argument("--text")
    parser.add_argument("--watcher")
    parser.add_argument("--line")
    parser.add_argument("--resolve")
    parser.add_argument("--is-green", dest="is_green")
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    named = [v for v in (args.key, args.resolve, args.is_green) if v is not None]
    modes = len(named) + sum(1 for f in (args.drain, args.open, args.selftest) if f)
    if modes != 1:
        parser.error("нужен ровно один режим: --key/--resolve/--is-green/--drain/--open/--selftest")
    for value in named:
        if not valid_key(value):
            parser.error(f"ключ «{value[:30]}» не по форме <сторож>:<род беды> (≤{KEY_MAX} знаков)")
    if args.key is not None and not args.title:
        parser.error("--key требует --title")
    return args


def main(argv: list) -> int:
    args = parse_args(argv)
    if args.selftest:
        return run_selftest()
    paths = Paths()
    if args.key is not None:
        return command_raise(paths, args)
    if args.resolve is not None:
        return command_resolve(paths, args.resolve)
    if args.is_green is not None:
        return command_is_green(paths, args.is_green)
    if args.open:
        return command_open(paths)
    return command_drain(paths)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
