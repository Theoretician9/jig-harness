#!/usr/bin/env python3
"""Проба сторожа меток: придержанное лимитом видно в утренней сводке числом.

Зачем. Дверь канала (tg_send.sh) пишет каждое придержанное лимитом подписки
сообщение в $LOG_DIR/придержано-лимитом.jsonl, и этот файл не читает никто:
сколько вестей правило проглотило за сутки, владелец не видит. Карточка
hr.priderzhannoe-ne-vidno-v-svodke: в сводке heartbeat-watch строка
«придержано лимитом за сутки N, из них без досыла M».

  H1 — в журнале три свежие записи, у одной «досыл»: false — в сводке строка
       «придержано лимитом за сутки 3, из них без досыла 1»;
  H2 — журнала нет — строки «придержано» в сводке нет;
  H3 — журнал пуст — строки «придержано» в сводке нет.
  H4 — запись 25 ч назад рядом со свежими не считается (окно 24 ч);
  H5 — запись с ts на 3 суток вперёд не считается (верхняя граница окна);
  H6 — битая строка, `[1]`, `{"ts":5}`, запись без ts среди годных:
       считаются только годные, прогон жив;
  H7 — байт не-UTF-8 в одной строке не губит соседние: годные посчитаны;
  H8 — все записи старше суток — строки нет (или «за сутки 0»);
  H9 — запись без ключа «досыл» в «без досыла» не попадает: писатель
       пишет bool всегда, без ключа — не «досыл: false»;
  H10 — файл больше водораздела (64 МиБ — выше потолка 60 МБ, который
       retention.yaml даёт *.jsonl; карточка числа не называет): сводка
       говорит о размере, а не молча считает весь файл;
  R1 — имя придержано-лимитом.jsonl названо в маске уборки (retention.yaml
       или tmp-sweep.yaml), а не только под общей «*.jsonl».

Стенд — временный каталог: копия демона в раскладке проекта, паспорт и
LOG_DIR стенда, HEARTBEAT_DRY_SEND=1 (сводка в вывод, дверь не зовётся),
писатель тревог и дверь — заглушки, пишущие зов в файл стенда. Боевой
/etc/harness и /var/log/harness проба не читает и не пишет. Путь к демону —
дверь HEARTBEAT_WATCH_SH (мутация копии судит копию).

    python3 scripts/test_heartbeat_held_line.py
Код возврата: 0 — все пути прошли; 1 — есть упавший.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# Две раскладки: проект (scripts/ рядом с harness/) и пакет (harness/scripts/).
ROOT = HERE.parent
DEMON = Path(os.environ.get("HEARTBEAT_WATCH_SH") or next(
    (p for p in (ROOT / "harness" / "demons" / "heartbeat-watch.sh",
                 ROOT / "demons" / "heartbeat-watch.sh") if p.is_file()),
    ROOT / "harness" / "demons" / "heartbeat-watch.sh"))
HELD = "придержано-лимитом.jsonl"
# Выше потолка, который retention.yaml даёт *.jsonl: любой разумный водораздел ниже.
BIG_BYTES = 64 * 1024 * 1024
HELD_LINE = re.compile(r"придержано лимитом за сутки\s+(\d+)\D+?без досыла\s+(\d+)", re.IGNORECASE)
TIMEOUT = 300

paths = failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global paths, failures
    paths += 1
    if ok:
        print(f"ок  · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name}: {detail[:900]}")


# Заглушки пишут зов в файл стенда: так видно, что стенд не вышел наружу.
STUB = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> {calls!r}
exit 0
'''


def build_stand(stand: Path) -> tuple[list, dict]:
    tree = stand / "tree"
    (tree / "harness" / "demons").mkdir(parents=True)
    shutil.copy2(DEMON, tree / "harness" / "demons" / "heartbeat-watch.sh")
    # Библиотеки рядом с пробой: верно в обеих раскладках и при подмене демона.
    shutil.copytree(HERE / "lib", tree / "scripts" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(HERE / "language-level.py", tree / "scripts" / "language-level.py")
    for name in ("alert-task.py", "tg_send.sh"):
        stub = tree / "scripts" / name
        stub.write_text(STUB.format(calls=str(stand / "outside-calls.log")), encoding="utf-8")
        stub.chmod(0o755)
    log_dir, hb = stand / "log", stand / "hb"
    log_dir.mkdir()
    hb.mkdir()
    conf = stand / "install.conf"
    conf.write_text(
        f'PROJECT_NAME="stand"\nPROJECT_DIR="{tree}"\nAGENT_USER="stand"\n'
        f'SECRETS_DIR="{stand}"\nTG_TOKEN_FILE="{stand}/no-token"\nTG_CHAT_ID=""\n'
        f'AUTONOMY="semi"\nHEARTBEAT_DIR="{hb}"\nLOG_DIR="{log_dir}"\n'
        f'TG_CHANNEL_VARIANT="A"\n', encoding="utf-8")
    (stand / "harness.conf").write_text('HEARTBEAT_PERIODS="alpha:1440"\n', encoding="utf-8")
    for name in (".install-stamp", "alpha"):
        (hb / name).touch()
    home = stand / "home"
    home.mkdir()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "LC_ALL": "C.UTF-8",
           "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "PYTHONDONTWRITEBYTECODE": "1",
           "HARNESS_INSTALL_CONF": str(conf), "HARNESS_CONF": str(stand / "harness.conf"),
           "HEARTBEAT_DRY_SEND": "1"}
    return ["bash", str(tree / "harness" / "demons" / "heartbeat-watch.sh")], env


_ABSENT = object()


def held_record(minutes_ago: int, resend=_ABSENT, text: str = "") -> str:
    """Запись в формате двери канала (tg_send.sh, ветка придержания).

    Отрицательные минуты — ts в будущем; resend не задан — ключа «досыл» нет."""
    ts = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    record = {"ts": ts, "script": "tg_send",
              "why": "держится лимит подписки — владельцу молчим до обновления",
              "sbros": "", "text": text or f"весть {minutes_ago}"}
    if resend is not _ABSENT:
        record["досыл"] = resend
    return json.dumps(record, ensure_ascii=False) + "\n"


def run(cmd: list, env: dict) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "таймаут"
    return p.returncode, p.stdout + p.stderr


def held_lines(out: str) -> list:
    return [line for line in out.splitlines() if "придержано" in line.lower()]


def case(tmp: Path, tag: str, held: str | bytes | None) -> tuple[int, str, Path]:
    stand = tmp / tag
    stand.mkdir()
    demon, env = build_stand(stand)
    if held is not None:
        (stand / "log" / HELD).write_bytes(held if isinstance(held, bytes) else held.encode("utf-8"))
    rc, out = run(demon, env)
    check(f"{tag}·стенд жив: прогон прошёл, сводка в выводе",
          rc == 0 and "Утренняя сводка" in out, f"rc={rc}; вывод: {out[-700:]}")
    check(f"{tag}·стенд не вышел наружу: ни писателя тревог, ни двери",
          not (stand / "outside-calls.log").exists() and "/var/log/harness" not in out,
          f"вывод: {out[-400:]}")
    return rc, out, stand


def case_held_counted(tmp: Path) -> None:
    print("— H1: три свежих придержанных, одно без досыла — строка с числами —")
    rc, out, _ = case(tmp, "H1", held_record(30, True) + held_record(300, False)
                      + held_record(600, True))
    found = [m.groups() for m in map(HELD_LINE.search, out.splitlines()) if m]
    check("H1·в сводке «придержано лимитом за сутки 3, из них без досыла 1»",
          found == [("3", "1")],
          f"найдено {found}; строки со словом «придержано»: {held_lines(out)}; вывод: {out[-500:]}")


def case_no_held(tmp: Path, tag: str, held: str | None, what: str) -> None:
    print(f"— {tag}: {what} — строки «придержано» нет —")
    rc, out, _ = case(tmp, tag, held)
    check(f"{tag}·{what}: в сводке нет строки «придержано»", rc == 0 and not held_lines(out),
          f"rc={rc}; строки: {held_lines(out)}")


def counted(out: str) -> list:
    return [m.groups() for m in map(HELD_LINE.search, out.splitlines()) if m]


def case_count(tmp: Path, tag: str, held: str | bytes, what: str, want: tuple) -> None:
    print(f"— {tag}: {what} — в сводке {want} —")
    rc, out, _ = case(tmp, tag, held)
    found = counted(out)
    check(f"{tag}·{what}: «за сутки {want[0]}, без досыла {want[1]}»",
          rc == 0 and found == [want],
          f"rc={rc}; найдено {found}; строки: {held_lines(out)}; вывод: {out[-400:]}")


def case_all_old(tmp: Path) -> None:
    print("— H8: все записи старше суток — строки нет или «за сутки 0» —")
    rc, out, _ = case(tmp, "H8", held_record(25 * 60, False) + held_record(48 * 60, True))
    found = counted(out)
    check("H8·все записи старше суток: не посчитаны",
          rc == 0 and (not held_lines(out) or found == [("0", "0")]),
          f"rc={rc}; найдено {found}; строки: {held_lines(out)}")


def case_big(tmp: Path) -> None:
    print("— H10: файл больше водораздела — сводка говорит о размере, весь файл не считает —")
    chunk = held_record(60, True, text="x" * (64 * 1024))
    total = BIG_BYTES // len(chunk.encode("utf-8")) + 1
    rc, out, _ = case(tmp, "H10", chunk * total)
    lines = held_lines(out)
    found = counted(out)
    told = any(re.search(r"водораздел|хвост|больше|МБ|МиБ|MB|не чит", line, re.IGNORECASE)
               for line in lines)
    check("H10·файл больше водораздела: строка «придержано» называет размер/хвост",
          rc == 0 and told and all(int(n) < total for n, _ in found),
          f"rc={rc}; записей в файле {total}; найдено {found}; строки: {lines}")


def config_dir() -> Path:
    return next((p for p in (ROOT / "harness" / "config", ROOT / "config") if p.is_dir()),
                ROOT / "harness" / "config")


def case_sweep_mask() -> None:
    print("— R1: имя журнала придержанного в маске уборки —")
    conf = config_dir()
    retention = yaml.safe_load((conf / "retention.yaml").read_text(encoding="utf-8"))
    masks = [str(p.get("маска", "")) for p in retention.get("пути") or []]
    sweep = (conf / "tmp-sweep.yaml").read_text(encoding="utf-8")
    named = [m for m in masks if HELD in m.split("|")]
    check("R1·retention.yaml разобран: масок больше нуля", bool(masks), f"{conf}")
    check(f"R1·{HELD} назван в маске retention.yaml или в tmp-sweep.yaml",
          bool(named) or HELD in sweep, f"масок {len(masks)}, с именем: {named}")


def main() -> int:
    print(f"демон: {DEMON}")
    if not DEMON.is_file():
        print(f"ПЛОХО · демона нет: {DEMON}\nитого путей 0, неудач 1")
        return 1
    with tempfile.TemporaryDirectory(prefix="heartbeat-held-") as tmp:
        os.chmod(tmp, 0o700)
        tmp_path = Path(tmp).resolve()
        for name, fn in (("H1", lambda: case_held_counted(tmp_path)),
                         ("H2", lambda: case_no_held(tmp_path, "H2", None, "журнала придержанного нет")),
                         ("H3", lambda: case_no_held(tmp_path, "H3", "", "журнал придержанного пуст")),
                         ("H4", lambda: case_count(
                             tmp_path, "H4", held_record(30, True) + held_record(25 * 60, False)
                             + held_record(600, False) + held_record(23 * 60, True),
                             "запись 25 ч назад, запись 23 ч назад в счёте", ("3", "1"))),
                         ("H5", lambda: case_count(
                             tmp_path, "H5", held_record(30, False) + held_record(-3 * 24 * 60, False)
                             + held_record(60, True), "ts на 3 суток вперёд", ("2", "1"))),
                         ("H6", lambda: case_count(
                             tmp_path, "H6", held_record(30, False) + "не json\n[1]\n{\"ts\": 5}\n"
                             + '{"script": "tg_send", "досыл": false}\n' + held_record(90, True),
                             "битые строки среди годных", ("2", "1"))),
                         ("H7", lambda: case_count(
                             tmp_path, "H7", held_record(30, False).encode("utf-8")
                             + b'{"ts": "\xff\xfe", "text": "\xff"}\n'
                             + held_record(90, True).encode("utf-8"),
                             "байт не-UTF-8 в одной строке", ("2", "1"))),
                         ("H8", lambda: case_all_old(tmp_path)),
                         ("H9", lambda: case_count(
                             tmp_path, "H9", held_record(30) + held_record(90, False),
                             "запись без ключа «досыл»", ("2", "1"))),
                         ("H10", lambda: case_big(tmp_path)),
                         ("R1", case_sweep_mask)):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — упавший стенд — неудача пробы, а не зелёный
                check(f"{name}: стенд собран", False, f"{type(exc).__name__}: {exc}")
    print(f"итого путей {paths}, неудач {failures}")
    return 0 if failures == 0 and paths > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
