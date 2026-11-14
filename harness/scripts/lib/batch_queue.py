#!/usr/bin/env python3
"""Очередь заявок с окном сбора: тяжёлый прогон — пачками, а не по одному.

Слово владельца 14.11.2026: «прогон и мутации — всё пачками». Замок на
одновременность ловил только второй заход ВО ВРЕМЯ первого, а заходы подряд
по одному id шли каждый своим прогоном. Здесь пачка — свойство кода, как у
merge queue и batching в CI: заявка не гонит прогон сама, а встаёт в очередь;
один заявитель становится исполнителем, ждёт окно сбора от самой старой заявки
и гонит ОДНИМ заходом все накопленные id (не больше потолка). Заявки, пришедшие,
пока заход идёт, уходят следующим заходом без ожидания окна — они старше его.

Каждый ждущий забирает итог захода по своему знаку. Падение захода — его код
всем заявкам пачки; смерть исполнителя видит следующий, взявший замок
исполнителя: взятые и не дописанные заявки получают отказ. Предел ожидания —
отказ и отзыв ещё не взятой заявки.

Место очереди — каталог: очередь.json под замком очередь.lock, замок
исполнителя исполнитель.lock и заход.log — построчный ход текущего захода.

    python3 batch_queue.py --место DIR --окно С --потолок N --ждать С ID[,ID] -- команда…
Команде последним доводом приходят id пачки через запятую.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BUSY_RC = 75
POLL_SEC = 1.0
RUN_LOG = "заход.log"


@contextlib.contextmanager
def _locked(place: Path, name: str, wait: bool = True):
    """Замок-файл в месте очереди; отдаёт True, если взят."""
    place.mkdir(parents=True, exist_ok=True)
    with open(place / name, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


@contextlib.contextmanager
def _state(place: Path):
    """Состояние очереди под её замком; изменённое пишется атомарно."""
    with _locked(place, "очередь.lock"):
        path = place / "очередь.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("заявки", [])
        state.setdefault("итоги", {})
        yield state
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def submit(place: Path, ids: list[str]) -> str:
    token = f"{os.getpid()}-{time.time_ns()}"
    with _state(place) as state:
        state["заявки"].append({"знак": token, "ids": ids, "с": time.time(), "заход": None})
    return token


def take_result(place: Path, token: str) -> dict | None:
    with _state(place) as state:
        return state["итоги"].pop(token, None)


def withdraw(place: Path, token: str) -> None:
    """Заявитель ушёл: не взятая заявка снимается, чтобы её не гнали впустую."""
    with _state(place) as state:
        state["заявки"] = [з for з in state["заявки"]
                           if not (з["знак"] == token and з["заход"] is None)]


def _close(state: dict, tokens: list[str], code: int, lines: list[str]) -> None:
    for token in tokens:
        state["итоги"][token] = {"код": code, "строки": lines}
    state["заявки"] = [з for з in state["заявки"] if з["знак"] not in tokens]


def fail_orphans(place: Path) -> None:
    """Зовётся под замком исполнителя: взятые и не закрытые заявки — от умершего."""
    with _state(place) as state:
        orphans = [з["знак"] for з in state["заявки"] if з["заход"]]
        _close(state, orphans, BUSY_RC,
               ["очередь: заход умер, не дописав итог — заявка не прогнана, подай её снова"])


def take_batch(place: Path, window: float, cap: int) -> tuple[list[str], list[str]] | float:
    """(знаки, id) пачки, взятой в заход; число — сколько ещё ждать окна."""
    with _state(place) as state:
        pending = [з for з in state["заявки"] if з["заход"] is None]
        if not pending:
            return 0.0
        left = pending[0]["с"] + window - time.time()
        if left > 0:
            return left
        tokens: list[str] = []
        ids: list[str] = []
        for request in pending:
            fresh = [i for i in request["ids"] if i not in ids]
            if tokens and len(ids) + len(fresh) > cap:
                break
            tokens.append(request["знак"])
            ids += fresh
            request["заход"] = os.getpid()
        return tokens, ids


def _to_stderr(line: str) -> None:
    sys.stderr.write(line)
    sys.stderr.flush()


def execute(command: list[str], ids: list[str], place: Path, echo=_to_stderr) -> tuple[int, list[str]]:
    """Заход: строки исполнителя идут ПО МЕРЕ вывода в заход.log места очереди и
    в echo исполнителя — ход видно, пока заход идёт, и убитый по пределу заход
    оставляет след; итог собирается весь."""
    lines: list[str] = []
    try:
        proc = subprocess.Popen(command + [",".join(ids)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
    except OSError as error:
        return 127, [f"очередь: исполнитель не запустился: {error}"]
    with open(place / RUN_LOG, "w", encoding="utf-8") as run_log:
        run_log.write(f"заход {','.join(ids)} с {time.strftime('%d.%m %H:%M:%S')}\n")
        run_log.flush()
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            run_log.write(line)
            run_log.flush()
            echo(line)
    code = proc.wait()
    if code:
        lines.append(f"очередь: заход {','.join(ids)} кончился кодом {code}")
    return code, lines


def serve(place: Path, token: str, command: list[str], window: float, cap: int,
          deadline: float, echo=_to_stderr) -> dict | None:
    """Исполнитель: гонит пачки, пока своя заявка не получит итог или не выйдет срок."""
    fail_orphans(place)
    while time.monotonic() < deadline:
        result = take_result(place, token)
        if result:
            return result
        batch = take_batch(place, window, cap)
        if isinstance(batch, float):
            time.sleep(min(max(batch, 0.05), POLL_SEC))
            continue
        tokens, ids = batch
        code, lines = BUSY_RC, ["очередь: исполнитель упал посреди захода"]
        try:
            code, lines = execute(command, ids, place, echo)
        finally:
            with _state(place) as state:
                _close(state, tokens, code, lines)
    return None


def request(ids: list[str], command: list[str], place: Path, window: float, cap: int,
            wait: float, echo=_to_stderr) -> tuple[int, list[str]]:
    """Заявка: (код захода, строки его вывода). Отказ по сроку — BUSY_RC.
    echo получает строки захода по мере вывода, если заход гонит этот заявитель."""
    token = submit(place, ids)
    print(f"очередь: заявка {','.join(ids)} подана; ход захода — {place / RUN_LOG}",
          file=sys.stderr, flush=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        result = take_result(place, token)
        if result is None:
            with _locked(place, "исполнитель.lock", wait=False) as mine:
                if mine:
                    result = serve(place, token, command, window, cap, deadline, echo)
        if result:
            return result["код"], result["строки"]
        time.sleep(POLL_SEC)
    withdraw(place, token)
    result = take_result(place, token)
    if result:
        return result["код"], result["строки"]
    return BUSY_RC, [f"очередь: не дождались итога за {wait:.0f} с — заявка {','.join(ids)} снята"]


def main(argv: list[str]) -> int:
    split = argv.index("--")
    head, command = argv[:split], argv[split + 1:]
    opts = dict(zip(head[0::2], head[1::2]))
    ids = [i for i in head[-1].split(",") if i]
    code, lines = request(ids, command, Path(opts["--место"]), float(opts["--окно"]),
                          int(opts["--потолок"]), float(opts["--ждать"]))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
