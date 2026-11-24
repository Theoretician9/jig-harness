#!/usr/bin/env python3
"""Флаг «ротация ждёт идущих помощников» — ЧТЕНИЕ (спека ротация-ждёт-помощников, раздел 4).

Файл `$PANEL_STATE_DIR/rotation-waits-helpers.json` ставит сторож сессий. Хук
очереди и сторож читают его только здесь: два судьи одного
флага разошлись бы в том, что считать протухшим или битым.

Три ответа, а не два: «нет» | «живой» | «протух». Живость — mtime файла, а не
поле внутри: так она есть и у битого флага, который сторож всё равно освежает.
Битый, но живой флаг остаётся флагом (data пуст) — молчаливая смена не должна
получить зелёный свет из-за оборванной записи.

Запись и снятие — ТОЖЕ здесь (`--set`, `--clear`): сторож зовёт этот CLI, а не
собирает JSON сам; две точки записи разошлись бы в полях и правилах переноса.

`--set --stage S --helpers N --waited SEC [--forced 0|1] [--told EPOCH]
[--session ID] [--reason TEXT]` пишет флаг; «с», «причина», «принуждена» и
«сказано» переходят из прежнего флага: «с» нужен для отсчёта, «сказано» — чтобы
письмо владельцу не повторялось. `--clear` снимает флаг и метку «принято на
ожидании» (её ставит диспетчер канала).

Ротация с преемником (спека, раздел 14): этап «дорабатывает» — старая смена
доводит своих помощников, канал и входящие у преемника. К полям флага
добавлены «преемник», «tmux» (имя переименованной сессии старой смены),
«поручено», «с_доработки», «роль», «tmux_роли», «подробно»; все переходят из
прежнего флага, если не заданы. Флаг — по одному на tmux-сессию роли:
`rotation-waits-helpers.json` для сессии `TMUX_SESSION` (его имя знают хуки
копий), `rotation-waits-helpers.<tmux>.json` для прочих ролей. `state()` читает
флаг `TMUX_SESSION`; `state_for(session_id)` — любой флаг, где сессия — «сессия»
или «преемник». Ключ `ROTATE_SUCCESSOR=0` — путь отката: хуки не исполняют
этап «дорабатывает» (`successor_enabled()`).

Заявка ротации (`--request`, `--request-state`, `--request-clear`) — файл
`rotation-request.json` в том же каталоге: её ставят те, кто не ведёт ротацию
сам (обновление харнеса, признаки CLI и needrestart); сторож читает её как
«ротация нужна». Заявка — не флаг: блокера скорости не даёт.

`--state` печатает содержимое ЖИВОГО флага одной строкой JSON (последний взгляд
перед перезагрузкой в окне ротации). Нет флага или протух — пусто и код 1: хук
протухший флаг не исполняет, значит новые запуски помощников уже не держатся.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as конф                                     # noqa: E402

FLAG_NAME = "rotation-waits-helpers.json"
REQUEST_NAME = "rotation-request.json"
STALE_DEFAULT_MIN = 30
FINISHING = "дорабатывает"
# Поля, которые переходят из прежнего флага, если их не задали: сторож меняет
# этап и счётчики, а «когда началось», «кто преемник» помнит файл.
KEPT_FIELDS = ("преемник", "tmux", "поручено", "с_доработки", "роль", "tmux_роли", "подробно")


def folder() -> Path:
    """Каталог состояния панели; дверь стенда — HARNESS_PANEL_STATE, как у pause.py."""
    return Path(os.environ.get("HARNESS_PANEL_STATE") or конф.panel_state_dir())


def main_tmux() -> str:
    """tmux-сессия оркестратора. Строго из install.conf: во время ротации сторож
    временно подставляет в окружение имя старой сессии, и оно не должно стать «главной»."""
    return конф.из_файла(конф.путь_install_conf()).get("TMUX_SESSION") or "agent"


def flag_name(tmux_role: str = "") -> str:
    return FLAG_NAME if tmux_role in ("", main_tmux()) else f"rotation-waits-helpers.{tmux_role}.json"


def path(tmux_role: str = "") -> Path:
    return folder() / flag_name(tmux_role)


def request_path() -> Path:
    return folder() / REQUEST_NAME


def _setting(key: str) -> str:
    return os.environ.get(key) or конф.из_файла(конф.путь_harness_conf()).get(key) or ""


def stale_min() -> int:
    value = _setting("ROTATE_FLAG_STALE_MIN")
    return int(value) if value.strip().isdigit() else STALE_DEFAULT_MIN


def successor_enabled() -> bool:
    """ROTATE_SUCCESSOR=0 — путь отката: этап «дорабатывает» хуки и судья не исполняют (как до раздела 14)."""
    return _setting("ROTATE_SUCCESSOR").strip() != "0"


def _read(file: Path, now: float | None = None) -> tuple[str, dict]:
    try:
        age_sec = (now if now is not None else time.time()) - file.stat().st_mtime
    except OSError:
        return "нет", {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return ("протух" if age_sec > stale_min() * 60 else "живой"), data


def state(now: float | None = None, tmux_role: str = "") -> tuple[str, dict]:
    """(«нет» | «живой» | «протух», содержимое; пусто, если флаг не разобран) — флага `TMUX_SESSION`
    (у прочих ролей — `tmux_role`)."""
    return _read(path(tmux_role), now)


def state_for(session_id: str, now: float | None = None) -> tuple[str, dict]:
    """Флаг, где сессия — «сессия» (старая смена) или «преемник»: хукам нужен тот, что про них.
    Живой важнее протухшего; не нашёлся — («нет», {})."""
    found = ("нет", {})
    if not session_id:
        return found
    for file in sorted(folder().glob("rotation-waits-helpers*.json")):
        kind, data = _read(file, now)
        if session_id in (data.get("сессия"), data.get("преемник")) and (kind == "живой" or found[0] == "нет"):
            found = (kind, data)
    return found


def any_flag() -> bool:
    """Есть ли хоть один файл флага (любой роли): хук без флагов выходит, не разбирая stdin."""
    return any(folder().glob("rotation-waits-helpers*.json"))


def finishing_sessions(now: float | None = None) -> list[str]:
    """Сессии всех дорабатывающих смен с живым флагом (любой роли): их помощников перезапуск тоже оборвёт."""
    if not successor_enabled():
        return []
    found = []
    for file in sorted(folder().glob("rotation-waits-helpers*.json")):
        kind, data = _read(file, now)
        if kind == "живой" and data.get("этап") == FINISHING and data.get("сессия"):
            found.append(str(data["сессия"]))
    return found


def finishing_flag(session_id: str) -> dict | None:
    """Живой флаг «дорабатывает», если `session_id` — старая смена (канал и очередь у неё закрыты); иначе None.
    Пуст при ROTATE_SUCCESSOR=0."""
    if not successor_enabled():
        return None
    kind, data = state_for(session_id)
    if kind == "живой" and data.get("этап") == FINISHING and data.get("сессия") == session_id:
        return data
    return None


def _write(file: Path, data: dict) -> None:
    """Запись через временный файл и rename: хук читает файл в любой миг и не должен увидеть половину."""
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(f"{file.name}.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, file)
    finally:
        tmp.unlink(missing_ok=True)


def _old(file: Path) -> dict:
    try:
        old = json.loads(file.read_text(encoding="utf-8")) if file.stat().st_size else {}
    except (OSError, ValueError):
        old = {}
    return old if isinstance(old, dict) else {}


def put(stage: str, helpers: str, waited: int, forced: int | None,
        told: int | None, session: str, reason: str, *, successor: str | None = None,
        tmux: str | None = None, ordered: int | None = None, finishing_since: int | None = None,
        role: str | None = None, tmux_role: str = "", detail: str | None = None) -> None:
    now = int(time.time())
    file = path(tmux_role)
    old = _old(file)

    def kept(key, default):
        return default if old.get(key) is None else old[key]

    given = {"преемник": successor, "tmux": tmux, "поручено": ordered, "с_доработки": finishing_since,
             "роль": role, "tmux_роли": tmux_role or None, "подробно": detail}
    data = {"сессия": session, "этап": stage, "с": kept("с", now), "проверено": now,
            "ждали_сек": waited, "причина": kept("причина", reason),
            "помощников": int(helpers) if helpers != "?" else helpers,
            "принуждена": forced if forced is not None else kept("принуждена", 0),
            "сказано": told if told is not None else old.get("сказано")}
    for key in KEPT_FIELDS:
        value = given[key] if given[key] is not None else old.get(key)
        if value is not None:
            data[key] = value
    _write(file, data)


def clear(tmux_role: str = "") -> None:
    path(tmux_role).unlink(missing_ok=True)
    (folder() / "пауза-сказано-приём").unlink(missing_ok=True)


def put_request(reason: str, tmux: str, role: str, detail: str) -> None:
    """Заявка ротации; повторная заявка дописывает пути, а не теряет прежние (два обновления до прогона сторожа)."""
    old = _old(request_path())
    paths = [*dict.fromkeys([*str(old.get("подробно") or "").split(), *detail.split()])]
    _write(request_path(), {"причина": reason, "подробно": " ".join(paths), "tmux": tmux or main_tmux(),
                            "роль": role or "оркестратор", "с": old.get("с") or int(time.time())})


def request_state() -> dict | None:
    kind, data = _read(request_path())
    return data if kind != "нет" else None


def request_clear() -> None:
    request_path().unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rotation_flag.py")
    act = parser.add_mutually_exclusive_group(required=True)
    for name in ("state", "set", "clear", "request", "request-state", "request-clear"):
        act.add_argument(f"--{name}", action="store_true")
    parser.add_argument("--stage")
    parser.add_argument("--helpers")
    parser.add_argument("--waited", type=int)
    parser.add_argument("--forced", type=int, choices=(0, 1))
    parser.add_argument("--told", type=int)
    parser.add_argument("--session", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--successor")
    parser.add_argument("--tmux")
    parser.add_argument("--ordered", type=int)
    parser.add_argument("--finishing-since", type=int)
    parser.add_argument("--role")
    parser.add_argument("--tmux-role", default="")
    parser.add_argument("--detail")
    parser.add_argument("--for-session", help="с --state: флаг, где сессия — «сессия» или «преемник»")
    args = parser.parse_args(argv)
    if args.set:
        if args.stage is None or args.helpers is None or args.waited is None:
            parser.error("--set требует --stage, --helpers, --waited")
        # «?» — сторож не смог сосчитать помощников; всё прочее, кроме ASCII-цифр
        # («-1», «1.5», арабо-индийские, «²»), отвергается до записи флага.
        if args.helpers != "?" and not re.fullmatch(r"[0-9]+", args.helpers):
            parser.error("--helpers: ожидаются цифры 0-9 или «?»")
        put(args.stage, args.helpers, args.waited, args.forced, args.told, args.session, args.reason,
            successor=args.successor, tmux=args.tmux, ordered=args.ordered,
            finishing_since=args.finishing_since, role=args.role, tmux_role=args.tmux_role,
            detail=args.detail)
        return 0
    if args.clear:
        clear(args.tmux_role)
        return 0
    if args.request:
        if not args.reason:
            parser.error("--request требует --reason")
        put_request(args.reason, args.tmux or "", args.role or "", args.detail or "")
        return 0
    if args.request_clear:
        request_clear()
        return 0
    if args.request_state:
        data = request_state()
        if data is None:
            return 1
        print(json.dumps(data, ensure_ascii=False))
        return 0
    kind, data = state_for(args.for_session) if args.for_session else state(tmux_role=args.tmux_role)
    if kind != "живой":
        return 1
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
