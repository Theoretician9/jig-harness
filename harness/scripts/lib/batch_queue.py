#!/usr/bin/env python3
"""Одна очередь тяжёлых прогонов дня: мутации задач и мутации помощников.

Слово владельца 14.11.2026: «прогон и мутации — всё пачками». Слово 17.11.2026:
замки, очереди, прогоны — делать эффективнее. Заявка не гонит прогон сама и
не пропадает молча: она встаёт в очередь и живёт, пока не получит итог или не
станет «потерянной» с событием и тревогой.

Заходы гонит ОДИН исполнитель — `--дослужить`/`--исполнитель` под замком исполнитель.lock, взятым
без ожидания: занят — выходит сразу. Его пускает заявитель сразу после подачи
(отдельным процессом, переживающим заявителя) и строка cron heavy-queue раз в
10 минут — брошенная заявка не ждёт нового заявителя. Исполнитель берёт пачку
заявок одного (дерево, среда, команда), ждёт окно сбора от самой старой и гонит
ОДНИМ заходом все накопленные id (не больше потолка); заявки без id идут своим
заходом по одной команде. Заход идёт в `дерево` заявки с её `среда`.

Итог заявки судится по её id: «дыр N» или «шаг пуст» — итог; всё прочее, занятый
срез (код 75) и смерть исполнителя возвращают заявку в очередь. Занятый срез —
не попытка, остальное — попытка; после `попыток` попыток или `предел_возраста_ч`
заявка закрывается итогом «потеряна» и событием heavy.lost.

Место очереди — каталог ($LOG_DIR/очередь-мутаций): очередь.json под замком
очередь.lock, замок исполнителя исполнитель.lock и заход.log — построчный ход
текущего захода.

    python3 batch_queue.py --дослужить [--место DIR --окно С --потолок N]   (строка cron)
    python3 batch_queue.py --исполнитель [--место DIR --окно С --потолок N] (то же, без меток запуска)
    python3 batch_queue.py --подать --без-ids --дерево D [--ждать С] -- команда…
    python3 batch_queue.py [--место DIR --окно С --потолок N --ждать С] ID[,ID] -- команда…
Команде с id последним доводом приходят id пачки через запятую.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import events  # noqa: E402

SRC = "scripts/lib/batch_queue.py"
JOURNAL = "checks-slice"
BUSY_RC = 75
LOST_RC = 2
REFUSED_RC = 3
POLL_SEC = 1.0
RESPAWN_SEC = 5.0
RUN_LOG = "заход.log"
HISTORY_TIMEOUT_SEC = 900
HISTORY_UNREAD_RC = 2
RESULT_KEEP_SEC = 7 * 86400
ALERT_KEY = "heavy-runs:lost"
ALERT_EVENTS = ("heavy.lost", "heavy.requester-gone", "mutations.history-unread")
ALERT_WINDOW_SEC = 24 * 3600
# Ждать в очереди, а не в двери: на «занято» дверь не держит заход часами.
DOOR_LOCK_WAIT_MIN = "2"
DEFAULTS = {"задач": 6, "окно_сбора_сек": 0, "ждать_итога_мин": 300, "попыток": 3,
            "предел_возраста_ч": 24, "пауза_повтора_сек": 60}
# Те же имена, что переносит дверь среза (run-in-checks-slice.sh): заявка несёт среду
# заявителя, а не исполнителя. Файл истории сюда не входит — его ставит исполнитель.
ENV_NAMES = re.compile(r"(HARNESS_|MUTATION_|MUT_|MUTPROBE_|CHECKS_|NIGHTLY_)\w*|LOG_DIR|"
                       r"GIT_CEILING_DIRECTORIES|PATH|HOME|LANG|LC_\w+|TERM|TMPDIR|VIRTUAL_ENV")
ENV_SKIP = re.compile(r"HARNESS_(CHECKS_SLICE_|GIT_HISTORY$|IN_CHECKS_SLICE$)")
RESULT_LINE = re.compile(r"^мутации задачи (\S+): (.*)$")
PROGRESS_LINE = re.compile(r"^мутации задачи (\S+): не догнана, новых мутаций (\d+)")


def settings(root: Path | None = None) -> dict:
    import yaml
    path = (root or config.корень_дерева(__file__)) / "harness" / "config" / "pipeline.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        loaded = {}
    return {**DEFAULTS, **(loaded.get("пачка") or {})}


def request_env() -> dict:
    return {name: value for name, value in os.environ.items()
            if ENV_NAMES.fullmatch(name) and not ENV_SKIP.match(name) and "\n" not in value}


def default_place() -> Path:
    return Path(config.log_dir()) / "очередь-мутаций"


def _event(name: str, outcome: str, **data) -> None:
    events.emit(SRC, name, outcome, journal=JOURNAL, data=data)


def _git_top(where: Path) -> Path | None:
    done = subprocess.run(["git", "-C", str(where), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def _tree_of_command(command: list[str]) -> str:
    """Дерево заявки старого формата — корень git по первому существующему пути команды."""
    for part in command:
        path = Path(part)
        if path.is_absolute() and path.exists():
            top = _git_top(path if path.is_dir() else path.parent)
            if top:
                return str(top)
    return os.getcwd()


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


def _normalize(state: dict) -> None:
    """Заявки старого формата получают поля нового: очередь не падает на выкате."""
    state.setdefault("заявки", [])
    state.setdefault("итоги", {})
    for request in state["заявки"]:
        request.setdefault("попыток", 0)
        request.setdefault("не_раньше", 0)
        request.setdefault("среда", {})
        request.setdefault("взята", None)
        request.setdefault("дерево", None)
        if not request["дерево"]:
            request["дерево"] = _tree_of_command(request["команда"])


def _read(place: Path) -> dict:
    try:
        state = json.loads((place / "очередь.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    _normalize(state)
    return state


@contextlib.contextmanager
def _state(place: Path):
    """Состояние очереди под её замком; изменённое пишется атомарно."""
    with _locked(place, "очередь.lock"):
        state = _read(place)
        yield state
        tmp = (place / "очередь.json").with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(place / "очередь.json")


def _same_key(request: dict, command: list[str], tree: str, env: dict, has_ids: bool) -> bool:
    return (request["команда"] == command and request["дерево"] == tree
            and request["среда"] == env and bool(request["ids"]) == has_ids)


def submit(place: Path, ids: list[str], command: list[str], tree: str, env: dict) -> str:
    """Поставить заявку. Заявка без id с тем же (дерево, команда, среда) вторую не
    ставит: заявитель-прибор после 75 подаёт снова и ждёт знак первой."""
    with _state(place) as state:
        if not ids:
            same = next((з for з in state["заявки"] if _same_key(з, command, tree, env, False)), None)
            if same:
                return same["знак"]
        sign = f"{os.getpid()}-{time.time_ns()}"
        state["заявки"].append({"знак": sign, "ids": ids, "команда": command, "с": time.time(),
                                "заход": None, "дерево": tree, "среда": env, "попыток": 0,
                                "не_раньше": 0, "взята": None})
    return sign


def read_result(place: Path, sign: str) -> dict | None:
    """Итог не снимается: заявку без id ждут все заявители с тем же ключом."""
    return _read(place)["итоги"].get(sign)


def _close(state: dict, signs: list[str], code: int, lines: list[str]) -> None:
    for sign in signs:
        state["итоги"][sign] = {"код": code, "строки": lines, "когда": time.time()}
    state["заявки"] = [з for з in state["заявки"] if з["знак"] not in signs]


def _give_back(request: dict, attempt: bool, pause: float) -> None:
    request["заход"] = None
    request["взята"] = None
    request["не_раньше"] = time.time() + pause
    request["попыток"] += 1 if attempt else 0


def fail_orphans(place: Path) -> None:
    """Зовётся под замком исполнителя: взятые и не закрытые заявки — от умершего.
    Смерть — попытка заявке, чей заход оборвался; пауза не нужна."""
    with _state(place) as state:
        for request in state["заявки"]:
            if request["заход"]:
                _give_back(request, True, 0)


def _lost_reason(request: dict, conf: dict, now: float) -> str | None:
    if request["попыток"] >= int(conf["попыток"]):
        return f"{request['попыток']} попыток без итога"
    if now - request["с"] > float(conf["предел_возраста_ч"]) * 3600:
        return f"возраст больше {conf['предел_возраста_ч']} ч"
    return None


def _drop_lost(state: dict, conf: dict) -> list[tuple[dict, str]]:
    now = time.time()
    lost = [(з, _lost_reason(з, conf, now)) for з in state["заявки"] if з["заход"] is None]
    lost = [(з, reason) for з, reason in lost if reason]
    for request, reason in lost:
        _close(state, [request["знак"]], LOST_RC,
               [f"очередь: заявка потеряна — {reason}; заявитель увидит это тревогой"])
    return lost


def _pick(state: dict, window: float, cap: int) -> tuple[list[str], list[str], dict] | float | None:
    """Пачка из готовых заявок одного (дерево, среда, команда, «с id или без»)."""
    now = time.time()
    pending = [з for з in state["заявки"] if з["заход"] is None and з["не_раньше"] <= now]
    if pending:
        first = pending[0]
        pending = [з for з in pending if _same_key(з, first["команда"], first["дерево"], first["среда"], bool(first["ids"]))]
    if not pending:
        return None
    # Окно копит id; заявке без id копить нечего.
    window = window if pending[0]["ids"] else 0
    left = pending[0]["с"] + window - time.time()
    if left > 0:
        return left
    signs: list[str] = []
    ids: list[str] = []
    for request in pending:
        fresh = [i for i in request["ids"] if i not in ids]
        if signs and len(ids) + len(fresh) > cap:
            break
        signs.append(request["знак"])
        ids += fresh
        request["заход"] = os.getpid()
        request["взята"] = time.time()
    return signs, ids, pending[0]


def take_batch(place: Path, window: float, cap: int,
               conf: dict | None = None) -> tuple[list[str], list[str], dict] | float | None:
    """(знаки, id, заявка-образец) пачки, взятой в заход; число — сколько ещё ждать
    окна; None — готовых заявок нет. Здесь же судится «потеряна»."""
    with _state(place) as state:
        lost = _drop_lost(state, conf or settings())
        batch = _pick(state, window, cap)
    for request, reason in lost:
        _event("heavy.lost", "error", знак=request["знак"], ids=request["ids"],
               дерево=request["дерево"], попыток=request["попыток"], причина=reason)
    return batch


def tree_usable(tree: str) -> bool:
    """В дерево помощника без замка Claude Code заход не идёт: бумага в ушедшее дерево
    сделала бы его грязным навсегда. Основное дерево и не-git каталог годны."""
    where = Path(tree)
    if not where.is_dir():
        return False
    dirs = subprocess.run(["git", "-C", tree, "rev-parse", "--path-format=absolute", "--git-dir",
                           "--git-common-dir"], capture_output=True, text=True)
    paths = dirs.stdout.splitlines()
    if dirs.returncode != 0 or len(paths) != 2 or paths[0] == paths[1]:
        return True
    listing = subprocess.run(["git", "-C", tree, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True).stdout
    for block in listing.split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0] == f"worktree {where.resolve()}":
            return any(line.startswith("locked") for line in lines)
    return False


def release_gone(place: Path) -> None:
    """Заявки в ушедшие деревья закрываются событием heavy.requester-gone."""
    verdicts: dict[str, bool] = {}
    with _state(place) as state:
        for request in [з for з in state["заявки"] if з["заход"] is None]:
            usable = verdicts.setdefault(request["дерево"], tree_usable(request["дерево"]))
            if not usable:
                _close(state, [request["знак"]], LOST_RC,
                       [f"очередь: дерево заявки {request['дерево']} ушло — заявка закрыта, команда не звана"])
                _event("heavy.requester-gone", "skipped", знак=request["знак"], ids=request["ids"],
                       дерево=request["дерево"])


def _to_stderr(line: str) -> None:
    sys.stderr.write(line)
    sys.stderr.flush()


def execute(command: list[str], ids: list[str], place: Path, echo=_to_stderr,
            cwd: str | None = None, env: dict | None = None) -> tuple[int, list[str]]:
    """Заход: строки исполнителя идут ПО МЕРЕ вывода в заход.log места очереди и
    в echo исполнителя — ход видно, пока заход идёт, и убитый по пределу заход
    оставляет след; итог собирается весь."""
    lines: list[str] = []
    try:
        proc = subprocess.Popen(command + ([",".join(ids)] if ids else []), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1, cwd=cwd, env=env)
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


def record_history(tree: str, ids: list[str], target: Path, env: dict) -> str | None:
    """Историю git читает исполнитель вне среза (в срезе git ждёт память) и пишет файл
    для захода. None — файл записан; иначе причина."""
    script = Path(tree) / "scripts" / "acceptance.py"
    try:
        done = subprocess.run([sys.executable, str(script), "--история", ",".join(ids), "--в", str(target)],
                              cwd=tree, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, errors="replace", timeout=HISTORY_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError) as error:
        reason = type(error).__name__
    else:
        if done.returncode == 0:
            return None
        reason = f"код {done.returncode}: {(done.stdout + done.stderr).strip()[-300:]}"
        # Код 2 — acceptance сам не прочёл историю и уже поднял событие с причиной.
        if done.returncode == HISTORY_UNREAD_RC:
            return reason
    _event("mutations.history-unread", "error", ids=ids, дерево=tree, причина=reason)
    return reason


def _own_codes(ids: list[str], lines: list[str]) -> dict[str, int]:
    """Итоги id из вывода захода: «дыр N» — 1 при дырах, «шаг пуст» — 0; прочее — не итог."""
    codes: dict[str, int] = {}
    for line in lines:
        found = RESULT_LINE.match(line)
        if not found or found[1] not in ids:
            continue
        holes = re.match(r"дыр (\d+)", found[2])
        if holes:
            codes[found[1]] = 1 if int(holes[1]) else 0
        elif "шаг пуст" in found[2]:
            codes[found[1]] = 0
    return codes


def _progressed(lines: list[str]) -> set[str]:
    """id, которые недогнанным заходом сдвинулись: «не догнана, новых мутаций K», K > 0."""
    return {m[1] for m in map(PROGRESS_LINE.match, lines) if m and int(m[2])}


def settle(place: Path, signs: list[str], code: int, lines: list[str]) -> None:
    """Итог захода — заявкам пачки. 0 — всем закрыто; 75 — все вернулись без попытки;
    1 и 2 — каждая по своим id; иной код — всем. Заявка с id без итога возвращается без
    попытки, если заход дал ход по КАЖДОМУ такому id (новые мутации легли в кэш
    прибора, повтор доберёт остальное); иначе — с попыткой. От вечного круга страхует
    предел_возраста_ч."""
    pause = float(settings()["пауза_повтора_сек"])
    with _state(place) as state:
        for request in [з for з in state["заявки"] if з["знак"] in signs]:
            if code == BUSY_RC:
                _give_back(request, False, pause)
                continue
            own = _own_codes(request["ids"], lines)
            if code in (1, 2) and request["ids"] and set(request["ids"]) - set(own):
                lost = set(request["ids"]) - set(own)
                _give_back(request, not lost <= _progressed(lines), pause)
                continue
            own_code = max(own.values()) if code in (1, 2) and own else code
            _close(state, [request["знак"]], own_code, lines)


def run_batch(place: Path, signs: list[str], ids: list[str], sample: dict, echo=_to_stderr) -> None:
    env = {**os.environ, **sample["среда"], "MUTATION_LOCK_WAIT_MIN": DOOR_LOCK_WAIT_MIN}
    env.pop("HARNESS_GIT_HISTORY", None)
    history = place / f"история-{os.getpid()}.json"
    code, lines = BUSY_RC, ["очередь: исполнитель упал посреди захода"]
    try:
        problem = record_history(sample["дерево"], ids, history, env) if ids else None
        if problem:
            code = LOST_RC
            lines = [f"мутации задачи {i}: история не прочитана ({problem})" for i in ids]
        else:
            if ids:
                env["HARNESS_GIT_HISTORY"] = str(history)
            code, lines = execute(sample["команда"], ids, place, echo, sample["дерево"], env)
    finally:
        history.unlink(missing_ok=True)
        settle(place, signs, code, lines)


def _purge_results(place: Path) -> None:
    with _state(place) as state:
        limit = time.time() - RESULT_KEEP_SEC
        state["итоги"] = {k: v for k, v in state["итоги"].items() if v.get("когда", time.time()) > limit}


def drain(place: Path, window: float, cap: int, echo=_to_stderr) -> None:
    """Под замком исполнителя: гонит готовые заявки, пока они есть."""
    fail_orphans(place)
    while True:
        release_gone(place)
        batch = take_batch(place, window, cap)
        if batch is None:
            break
        if isinstance(batch, float):
            time.sleep(min(max(batch, 0.05), POLL_SEC))
            continue
        run_batch(place, *batch, echo)
    _purge_results(place)


def _last_alert_event() -> float | None:
    path = Path(events.log_dir()) / f"{JOURNAL}.jsonl"
    stamps = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
            if row.get("event") in ALERT_EVENTS:
                stamps.append(datetime.datetime.fromisoformat(row["ts"]).timestamp())
        except (ValueError, KeyError, TypeError):
            continue
    return max(stamps) if stamps else None


def judge_alarm() -> None:
    """Тревога по событиям очереди: свежее событие — поднять, 24 ч без них — погасить.
    Из сессии агента alert-task отказывает; тревогу ставит строка cron."""
    last = _last_alert_event()
    script = str(Path(__file__).resolve().parent.parent / "alert-task.py")
    if last and time.time() - last < ALERT_WINDOW_SEC:
        args = ["--key", ALERT_KEY, "--watcher", "heavy-queue",
                "--title", "Тяжёлые прогоны: заявка потеряна или не прочитана история",
                "--text", "Очередь тяжёлых прогонов закрыла заявку без прогона (heavy.lost, "
                          "heavy.requester-gone или mutations.history-unread за сутки): "
                          "python3 scripts/events.py refusals; ход — $LOG_DIR/очередь-мутаций/заход.log."]
    else:
        args = ["--resolve", ALERT_KEY]
    subprocess.run([sys.executable, script, *args], stdin=subprocess.DEVNULL,
                   capture_output=True, timeout=120)


def serve_ready(place: Path, window: float, cap: int) -> int:
    """`--дослужить`: гонит готовые заявки и выходит. В срезе — отказ: заход из среза
    ушёл бы под потолок среза дважды."""
    if os.environ.get("HARNESS_IN_CHECKS_SLICE"):
        _event("heavy.serve-refused", "refused", причина="вызов из среза проверок")
        print("очередь: --дослужить из среза проверок отказан — заходы гонит исполнитель вне среза",
              file=sys.stderr)
        return REFUSED_RC
    with _locked(place, "исполнитель.lock", wait=False) as mine:
        if mine:
            drain(place, window, cap)
            judge_alarm()
    return 0


def spawn_runner(place: Path, window: float, cap: int) -> subprocess.Popen:
    """Исполнитель отдельным процессом в своей сессии: смерть заявителя заход не обрывает."""
    place.mkdir(parents=True, exist_ok=True)
    with open(place / "исполнитель.log", "a", encoding="utf-8") as log:
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--исполнитель",
                                 "--место", str(place), "--окно", str(window), "--потолок", str(cap)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


class RunLogTail:
    """Ход захода своей заявки — заявителю по мере вывода: журнал, написанный до взятия
    заявки, — чужой заход, его не показываем."""

    def __init__(self, place: Path) -> None:
        self.path = place / RUN_LOG
        self.offset = 0

    def pump(self, taken_at: float | None, echo) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            return
        if taken_at is None or stat.st_mtime < taken_at:
            return
        if stat.st_size < self.offset:
            self.offset = 0
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
        text = chunk.decode("utf-8", errors="replace")
        complete = text[:text.rfind("\n") + 1]
        self.offset += len(complete.encode("utf-8"))
        for piece in complete.splitlines(keepends=True):
            echo(piece)


def await_result(place: Path, sign: str, window: float, cap: int, wait: float, echo) -> dict | None:
    deadline = time.monotonic() + wait
    tail, runner, started = RunLogTail(place), None, 0.0
    while time.monotonic() < deadline:
        state = _read(place)
        mine = next((з for з in state["заявки"] if з["знак"] == sign), None)
        tail.pump(mine["взята"] if mine else None, echo)
        if sign in state["итоги"]:
            return state["итоги"][sign]
        if runner is None or (runner.poll() is not None and time.monotonic() - started > RESPAWN_SEC):
            runner, started = spawn_runner(place, window, cap), time.monotonic()
        time.sleep(POLL_SEC)
    return read_result(place, sign)


def _switch() -> str:
    """Выключатель фоновых процессов: исполнителя гонит cron heavy-queue, и при «off» он
    молчит — заявитель должен знать, что заявка ждёт, а не пропала."""
    value = config.конфиг(config.путь_harness_conf()).get("BACKGROUND_PROCESSES")
    return os.environ.get("BACKGROUND_PROCESSES") or value or "on"


def request(ids: list[str], command: list[str], place: Path, window: float, cap: int,
            wait: float, echo=_to_stderr, tree: str | None = None) -> tuple[int, list[str]]:
    """Заявка: (код захода, строки его вывода). Вышел срок — BUSY_RC, заявка остаётся в
    очереди: её дослужит исполнитель. echo получает строки захода по мере вывода."""
    sign = submit(place, ids, command, tree or os.getcwd(), request_env())
    label = ",".join(ids) or " ".join(command[-2:])
    print(f"очередь: заявка {label} подана; ход захода — {place / RUN_LOG}", file=sys.stderr, flush=True)
    if _switch() != "on":
        print("очередь: выключатель off — заявка ждёт в очереди", file=sys.stderr, flush=True)
    result = await_result(place, sign, window, cap, wait, echo)
    if result:
        return result["код"], result["строки"]
    return BUSY_RC, [f"очередь: не дождались итога за {wait:.0f} с — заявка {label} в очереди, "
                     "дослужит heavy-queue"]


def _parse(argv: list[str]) -> tuple[set[str], dict[str, str], list[str], list[str]]:
    split = argv.index("--") if "--" in argv else len(argv)
    head, command = argv[:split], argv[split + 1:]
    flags = {"--подать", "--дослужить", "--исполнитель", "--без-ids"}
    opts: dict[str, str] = {}
    ids: list[str] = []
    rest = iter(a for a in head if a not in flags)
    for arg in rest:
        if arg.startswith("--"):
            opts[arg] = next(rest, "")
        else:
            ids = [i for i in arg.split(",") if i]
    return {a for a in head if a in flags}, opts, ids, command


def main(argv: list[str]) -> int:
    flags, opts, ids, command = _parse(argv)
    conf = settings()
    place = Path(opts["--место"]) if "--место" in opts else default_place()
    window = float(opts.get("--окно", conf["окно_сбора_сек"]))
    cap = int(opts.get("--потолок", conf["задач"]))
    if "--исполнитель" in flags:
        return serve_ready(place, window, cap)
    if "--дослужить" in flags:
        return events.run(SRC, lambda: serve_ready(place, window, cap), journal=JOURNAL, name="heavy-queue",
                          heartbeat="batch_queue")
    wait = float(opts.get("--ждать", float(conf["ждать_итога_мин"]) * 60))
    code, lines = request([] if "--без-ids" in flags else ids, command, place, window, cap, wait,
                          tree=opts.get("--дерево"))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
