#!/usr/bin/env python3
"""Пробы приоритета релиза: пока в карте незакрытая
карточка с полем «релиз: {версия, до}», новый прогон через дверь среза, чья
задача не релизная, получает отказ 75 с законным путём; ночные мутации ждут
конца релиза в пределах своего срока.

    python3 scripts/test_release_priority.py

Стенд — временный каталог: копия двери run-in-checks-slice.sh, lib/,
acceptance.py, mutation-probe.sh и ночного демона, своя карта dev-map.yaml,
записывающая корневая обёртка (Stand из test_checks_slice_door.py), свой
LOG_DIR. Живые карта, очередь среза и обёртка не трогаются.

Дверь для мутационного прогона:
    HARNESS_RELEASE_PRIORITY_SRC   каталог скриптов под пробой (по умолчанию свой)
Последняя строка: `приоритет релиза: N путей, F ПЛОХО`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "lib"))
import test_checks_slice_door as door_probe  # noqa: E402
from selftest_cases import Cases  # noqa: E402

SRC = Path(os.environ.get("HARNESS_RELEASE_PRIORITY_SRC") or HERE)
door_probe.SRC = SRC  # стенд двери копирует скрипты из того же каталога под пробой
BUSY_RC = 75
RELEASE_ID = "hr.проба-релиза"
OTHER_ID = "hr.проба-среза"  # Stand.acceptance зовёт с этим id
FAR = "2999-01-01T00:00+05:00"
PAST = "2000-01-01T00:00+05:00"

cases = Cases()
check = cases.check


def devmap(release: str | None, status: str = "wip") -> str:
    """Карта стенда: одна релизная карточка (или без поля) и одна чужая."""
    field = f"        релиз: {release}\n" if release else ""
    return ("version: 1\nprojects:\n  - id: harness\n    layer: infra\n    name: \"Харнес\"\n    tasks:\n"
            f"      - id: {RELEASE_ID}\n        status: {status}\n        title: \"релиз\"\n{field}"
            f"      - id: {OTHER_ID}\n        status: wip\n        title: \"чужая\"\n")


def release(until: str, version: str = "0.11.1") -> str:
    return f'{{ версия: "{version}", до: "{until}" }}'


def set_map(s, text: str) -> None:
    (s.root / "dev-map.yaml").write_text(text, encoding="utf-8")


def door(s, env: dict, *cmd: str):
    return s.run(["bash", str(s.root / "scripts" / "run-in-checks-slice.sh"), *cmd], env, s.work)


def acceptance(s, env: dict, ids: str):
    return s.run([sys.executable, str(s.root / "scripts" / "acceptance.py"), "--мутации-задачи", ids],
                 env, s.work)


def probe_door(s) -> None:
    """Дверь среза: отказ чужому прогону при живом релизе, проход — релизному и без релиза."""
    set_map(s, devmap(None))
    rc, _, recs = door(s, s.env(), "true")
    check("без релиза: прогон без задачи идёт, обёртка вызвана 1 раз", (0, 1), (rc, len(recs)))

    set_map(s, devmap(release(FAR)))
    rc, out, recs = s.acceptance(s.env())
    check("релиз идёт: мутации чужой задачи — отказ 75, обёртка не звана", (BUSY_RC, 0), (rc, len(recs)))
    check("отказ называет версию релиза и его id", True, "0.11.1" in out and RELEASE_ID in out)
    check("отказ называет срок «до» и законный путь HARNESS_TASK=", True,
          "2999" in out and "HARNESS_TASK=" in out)
    check("отказ записан событием checks.slice.release-yield", True,
          events(s))

    rc, _, recs = acceptance(s, s.env(), RELEASE_ID)
    check("релиз идёт: мутации релизной задачи проходят (обёртка 1 раз)", 1, len(recs))
    rc, out, recs = acceptance(s, s.env(), f"{RELEASE_ID},{OTHER_ID}")
    check("релиз идёт: пачка с чужой задачей — отказ 75 (релизный id не проводит чужой)",
          (BUSY_RC, 0), (rc, len(recs)))

    rc, _, recs = door(s, s.env(), "true")
    check("релиз идёт: прогон без задачи (HARNESS_TASK нет) — отказ 75", (BUSY_RC, 0), (rc, len(recs)))
    rc, _, recs = door(s, s.env(HARNESS_TASK="-"), "true")
    check("релиз идёт: HARNESS_TASK=- — задача неизвестна, отказ 75", (BUSY_RC, 0), (rc, len(recs)))
    rc, _, recs = door(s, s.env(HARNESS_TASK=OTHER_ID), "true")
    check("релиз идёт: HARNESS_TASK чужой задачи — отказ 75", (BUSY_RC, 0), (rc, len(recs)))
    rc, _, recs = door(s, s.env(HARNESS_TASK=RELEASE_ID), "true")
    check("законная дверь: HARNESS_TASK релизной задачи — проход", (0, 1), (rc, len(recs)))

    sliced = s.tmp / "cgroup-срез"
    sliced.write_text("0::/harness.slice/harness-checks.slice/run-u1.service\n", encoding="utf-8")
    rc, _, recs = door(s, s.env(HARNESS_CHECKS_SLICE_CGROUP_FILE=str(sliced)), "true")
    check("релиз идёт: вызов уже в срезе не судится (вложенный прогон держателя)", (0, 0), (rc, len(recs)))

    set_map(s, devmap(release(PAST)))
    rc, _, recs = door(s, s.env(), "true")
    check("срок «до» прошёл — релиз не держит, проход", (0, 1), (rc, len(recs)))
    for status in ("test", "done"):
        set_map(s, devmap(release(FAR), status=status))
        rc, _, recs = door(s, s.env(), "true")
        check(f"релизная карточка в {status} — релиз не держит, проход", (0, 1), (rc, len(recs)))
    set_map(s, devmap('{ версия: "0.11.1" }'))
    rc, out, recs = door(s, s.env(), "true")
    check("пометка без «до» не действует (проход) и судья предупреждает", (0, 1, True),
          (rc, len(recs), "до" in out and "ВНИМАНИЕ" in out))
    set_map(s, "projects: [ это не yaml\n")
    rc, out, recs = door(s, s.env(), "true")
    check("карта не читается — проход с предупреждением про карту", (0, 1, True),
          (rc, len(recs), "карта" in out and "ВНИМАНИЕ" in out))


def events(s) -> bool:
    logs = s.tmp / "logs"
    return any("checks.slice.release-yield" in f.read_text(encoding="utf-8", errors="replace")
               for f in logs.rglob("*") if f.is_file())


def yield_events(s) -> list:
    """Данные событий checks.slice.release-yield стенда."""
    found = []
    for f in (s.tmp / "logs").rglob("*"):
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if "checks.slice.release-yield" in line:
                try:
                    found.append(json.loads(line).get("data", {}))
                except ValueError:
                    found.append({"сырое": line})
    return found


def warned(out: str) -> bool:
    """Предупреждение о релизе, а не строка двери о подставленной двери пробы."""
    return any("ВНИМАНИЕ" in line and "дверь пробы" not in line for line in out.splitlines())


def probe_faults(tmp: Path) -> None:
    """Сбой судьи или карты — проход с «ВНИМАНИЕ», а не отказ всем прогонам."""
    s = door_probe.Stand(tmp / "bytes")
    (s.root / "dev-map.yaml").write_bytes(b"\xff\xfe\x00 \xc3(")
    rc, out, recs = door(s, s.env(), "true")
    check("карта в битой кодировке: дверь пропускает (обёртка 1 раз) с «ВНИМАНИЕ»", (0, 1, True),
          (rc, len(recs), warned(out)))
    check("карта в битой кодировке: событие release-yield не пишется", [], yield_events(s))

    s = door_probe.Stand(tmp / "raise")
    set_map(s, devmap(release(FAR)))
    judge = s.root / "scripts" / "lib" / "release_priority.py"
    judge.write_text("raise RuntimeError('проба: судья упал')\n", encoding="utf-8")
    rc, out, recs = door(s, s.env(), "true")
    check("судья упал: дверь пропускает (обёртка 1 раз) с «ВНИМАНИЕ», не 75", (0, 1, True),
          (rc, len(recs), warned(out)))
    check("судья упал: событие release-yield не пишется", [], yield_events(s))

    s = door_probe.Stand(tmp / "bare")
    set_map(s, devmap("{ версия: \"0.11.1\", до: 2999-01-01T10:00:00+05:00 }"))
    rc, _, recs = s.acceptance(s.env())
    check("«до» без кавычек: чужая задача — отказ 75", (BUSY_RC, 0), (rc, len(recs)))
    got = [e.get("до") for e in yield_events(s)]
    check("«до» без кавычек: в событии до= полное ISO-время", ["2999-01-01T10:00:00+05:00"], got)


def probe_fast_parse(s) -> None:
    """Карта 750 КБ разбирается libyaml (CSafeLoader), не чистым Python: на каждый
    вызов двери чистый разбор стоил ~1,7 с CPU. Судим загрузчик, а не секунды —
    на загруженной машине замер времени флейкает."""
    import yaml
    if not getattr(yaml, "__with_libyaml__", False):
        check("быстрый разбор: libyaml на машине нет — случай не судится", True, True)
        return
    set_map(s, devmap(release(FAR)))
    loaders = []
    real_load, real_safe = yaml.load, yaml.safe_load

    def load(stream, Loader=None, **kw):  # noqa: N803 — имя довода из yaml
        loaders.append(getattr(Loader, "__name__", str(Loader)))
        return real_load(stream, Loader=Loader, **kw)

    def safe_load(stream):
        loaders.append("safe_load")
        return real_safe(stream)
    import importlib.util
    spec = importlib.util.spec_from_file_location("release_priority_under_probe",
                                                  s.root / "scripts" / "lib" / "release_priority.py")
    module = importlib.util.module_from_spec(spec)
    saved = (sys.argv[:], os.environ.get("HARNESS_DEVMAP"))
    yaml.load, yaml.safe_load = load, safe_load
    try:
        os.environ["HARNESS_DEVMAP"] = str(s.root / "dev-map.yaml")
        sys.argv = ["release_priority.py", "--задачи", RELEASE_ID]
        spec.loader.exec_module(module)
        rc = module.main()
    finally:
        yaml.load, yaml.safe_load = real_load, real_safe
        sys.argv = saved[0]
        if saved[1] is None:
            os.environ.pop("HARNESS_DEVMAP", None)
        else:
            os.environ["HARNESS_DEVMAP"] = saved[1]
    check("быстрый разбор: судья ответил на релизную задачу 0", 0, rc)
    check("быстрый разбор: карта разобрана CSafeLoader и только им", ["CSafeLoader"], loaders)


def probe_judge(s) -> None:
    """Судья сам по себе: один носитель на дверь и ночного демона."""
    judge = s.root / "scripts" / "lib" / "release_priority.py"
    check("судья scripts/lib/release_priority.py есть", True, judge.is_file())
    if not judge.is_file():
        return
    set_map(s, devmap(release(FAR)))
    env = s.env()

    def ask(ids: str) -> int:
        return subprocess.run([sys.executable, "-I", str(judge), "--задачи", ids], env=env,
                              capture_output=True, text=True, timeout=30).returncode
    check("судья: релизная задача — 0", 0, ask(RELEASE_ID))
    check("судья: чужая — 75", BUSY_RC, ask(OTHER_ID))
    check("судья: пусто (задача неизвестна) — 75", BUSY_RC, ask(""))
    (s.root / "dev-map.yaml").write_bytes(b"\xff\xfe\x00 \xc3(")
    r = subprocess.run([sys.executable, "-I", str(judge), "--задачи", OTHER_ID], env=env,
                       capture_output=True, text=True, timeout=30)
    check("судья: карта в битой кодировке — сам отвечает 0 и «ВНИМАНИЕ» про карту, без трассировки",
          (0, True, False), (r.returncode, "ВНИМАНИЕ" in r.stderr and "карта" in r.stderr,
                             "Traceback" in r.stderr))


class Nightly:
    """Ночной демон на стенде: своя карта, подставная команда прибора пишет время старта."""

    def __init__(self, s) -> None:
        self.s = s
        demons = s.root / "harness" / "demons"
        demons.mkdir(parents=True, exist_ok=True)
        live = SRC.parent / "harness" / "demons" / "nightly-mutations.sh"
        if not live.is_file():
            live = SRC.parent / "demons" / "nightly-mutations.sh"
        self.daemon = demons / "nightly-mutations.sh"
        shutil.copy2(live, self.daemon)
        self.started = s.tmp / "прибор-старт"
        self.cmd = s.tmp / "pribor.sh"
        self.cmd.write_text(f"date +%s.%N > {self.started}\n"
                            "printf '=== мутационная проба: покраснели как надо 1, дыр 0, из кэша 0 ===\\n'\n",
                            encoding="utf-8")
        self.log = s.tmp / "nightly.log"
        for d in ("nlog", "hb"):
            (s.tmp / d).mkdir(exist_ok=True)

    def run(self, max_min: str, poll_s: str, timeout: int = 60) -> tuple[int, float]:
        conf = self.s.tmp / "nightly.conf"
        conf.write_text(f"NIGHTLY_MUTATIONS_MAX_MIN={max_min}\nNIGHTLY_MUTATIONS_ALERT_GAP_H=24\n"
                        f"NIGHTLY_MUTATIONS_LOCK_WAIT_MIN=0.02\nNIGHTLY_MUTATIONS_RELEASE_POLL_S={poll_s}\n",
                        encoding="utf-8")
        (self.s.tmp / "install.conf").write_text("", encoding="utf-8")
        self.started.unlink(missing_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("NIGHTLY_", "HARNESS_")) and k not in ("LOG_DIR", "PROJECT_DIR")}
        env.update(HEARTBEAT_DIR=str(self.s.tmp / "hb"), LOG_DIR=str(self.s.tmp / "nlog"),
                   HARNESS_LOG_DIR=str(self.s.tmp / "nlog"), PROJECT_DIR=str(self.s.root),
                   NIGHTLY_MUTATIONS_CMD=f"bash {self.cmd}", HARNESS_CONF=str(conf),
                   HARNESS_INSTALL_CONF=str(self.s.tmp / "install.conf"), HARNESS_HOME=str(self.s.tmp / "home"),
                   GIT_CEILING_DIRECTORIES=str(self.s.tmp))
        start = time.monotonic()
        try:
            r = subprocess.run(["bash", str(self.daemon)], env=env, cwd=str(self.s.work),
                               stdout=open(self.log, "w"), stderr=subprocess.STDOUT, timeout=timeout)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        return rc, time.monotonic() - start

    def started_at(self) -> float | None:
        if not self.started.is_file():
            return None
        return float(self.started.read_text().strip())

    def text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace") if self.log.is_file() else ""


def probe_nightly(s) -> None:
    n = Nightly(s)
    set_map(s, devmap(None))
    rc, _ = n.run("1", "1")
    check("ночные без релиза: прибор запущен сразу", True, n.started_at() is not None)

    set_map(s, devmap(release(FAR)))
    rc, took = n.run("0.1", "1")
    check("ночные при живом релизе: прибор НЕ запущен до конца срока", None, n.started_at())
    check("ночные при живом релизе: демон кончился сам в пределах срока (<40 с)", True, 0 <= rc and took < 40)
    check("ночные при живом релизе: журнал говорит «ждёт релиза» и версию", True,
          "релиз" in n.text() and "0.11.1" in n.text())

    until = dt.datetime.now().astimezone() + dt.timedelta(seconds=5)
    set_map(s, devmap(release(until.isoformat(timespec="seconds"))))
    rc, _ = n.run("1", "1")
    began = n.started_at()
    check("ночные: релиз кончился по «до» — прибор запущен", True, began is not None)
    check("ночные: прибор запущен не раньше «до» (ждал релиза)", True,
          began is not None and began >= until.timestamp() - 1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name, probe in (("дверь", lambda: probe_door(door_probe.Stand(tmp / "door"))),
                            ("судья", lambda: probe_judge(door_probe.Stand(tmp / "judge"))),
                            ("ночные", lambda: probe_nightly(door_probe.Stand(tmp / "nightly"))),
                            ("сбои", lambda: probe_faults(tmp / "faults")),
                            ("разбор", lambda: probe_fast_parse(door_probe.Stand(tmp / "parse")))):
            try:
                probe()
            except Exception as error:  # упавший стенд — красный путь, а не трассировка
                check(f"{name}: стенд отвечает", "ответ", f"{type(error).__name__}: {error}")
    print(f"приоритет релиза: {cases.paths} путей, {cases.failures} ПЛОХО")
    return 0 if cases.ok else 1


if __name__ == "__main__":
    sys.exit(main())
