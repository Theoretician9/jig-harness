#!/usr/bin/env python3
"""Пробы сторожа меток: самотест не выходит из стенда, лишние тревоги гаснут.

Зачем. Карточка heartbeat-watch:dead:beta родилась из самотеста: имя beta
есть только в его таблице периодов. Один вызов самотеста (недоставка сводки,
без HEARTBEAT_DRY_SEND) доходит до sync_tasks, а PROJECT_DIR окружения
(gates.sh ставит его для самотестов демонов) старше паспорта стенда — и
писатель тревог из живого дерева заводит карточку в боевом журнале. Сторож
такую карточку не погасит никогда: гасит он только имена своей таблицы.
Разбор: .tasks/hr.stale-alerts-03-11/отчёт.md.

  S1 — самотест на стенде: писатель тревог и дверь канала дерева кода не
       зовутся, «боевой» LOG_DIR стенда пуст;
  S2 — прогон сторожа: открытая тревога heartbeat-watch:dead:<имя> с именем
       вне HEARTBEAT_PERIODS гасится (--resolve), мёртвый из таблицы — нет,
       чужой сторож — нет;
  S3 — порог «мёртв» = период × множитель метки (HEARTBEAT_MULT_FILE):
       backup ×1 мёртв через 25 ч, nightly-gates ×2 жив через 25 ч и мёртв
       через 49 ч, нечитаемый файл множителей — громкий отказ.

Стенд — временный каталог: копия демона в раскладке проекта
(harness/demons + scripts/lib), писатель-заглушка scripts/alert-task.py и
дверь-заглушка scripts/tg_send.sh, пишущие каждый вызов в файл стенда.
«Боевой» корень установки — тоже в стенде (HARNESS_HOME); заглушка пишет журнал
только внутри стенда. Боевой /etc/harness и /var/log/harness проба не читает
и не пишет. Путь к демону — дверь HEARTBEAT_WATCH_SH (мутация копии судит
копию).

    python3 scripts/test_heartbeat_watch_alerts.py
Код возврата: 0 — все пути прошли; 1 — есть упавший.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Две раскладки: проект (scripts/ рядом с harness/) и пакет (harness/scripts/).
ROOT = HERE.parent
DEMON = Path(os.environ.get("HEARTBEAT_WATCH_SH") or next(
    (p for p in (ROOT / "harness" / "demons" / "heartbeat-watch.sh",
                 ROOT / "demons" / "heartbeat-watch.sh") if p.is_file()),
    ROOT / "harness" / "demons" / "heartbeat-watch.sh"))
JOURNAL = "тревоги-сторожей.jsonl"
TIMEOUT = 600

paths = failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global paths, failures
    paths += 1
    if ok:
        print(f"ок  · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name}: {detail[:900]}")


# Заглушка писателя: ведёт журнал тревог по-настоящему (рождение, зелёное,
# --open), но только если LOG_DIR лежит внутри стенда, и каждый зов пишет в
# CALLS вместе с тем, какой паспорт она бы прочла.
STUB_WRITER = '''#!/usr/bin/env python3
import json, os, re, sys
STAND = {stand!r}
CALLS = {calls!r}
conf = os.environ.get("HARNESS_INSTALL_CONF") or \\
    (os.environ.get("HARNESS_HOME") or "/etc/harness") + "/install.conf"
log_dir = ""
try:
    for line in open(conf, encoding="utf-8"):
        m = re.match(r'\\s*LOG_DIR="?([^"#]*)"?', line)
        if m:
            log_dir = m.group(1).strip()
except OSError:
    pass
with open(CALLS, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "conf": conf, "log_dir": log_dir}},
                        ensure_ascii=False) + "\\n")
if not log_dir.startswith(STAND + "/"):
    sys.exit(0)
journal = os.path.join(log_dir, {journal!r})
args = sys.argv[1:]
if args[:1] == ["--open"]:
    state = {{}}
    try:
        for line in open(journal, encoding="utf-8"):
            row = json.loads(line)
            state[row["ключ"]] = row["событие"]
    except OSError:
        pass
    for key, event in sorted(state.items()):
        if event != "зелёное":
            print(f"{{key}}\\thr.stand\\tповторов 0\\tс 2026-10-29")
    sys.exit(0)
if args[:1] in (["--key"], ["--resolve"]):
    key = args[1]
    event = "заведена" if args[0] == "--key" else "зелёное"
    os.makedirs(log_dir, exist_ok=True)
    with open(journal, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"ts": "2026-11-03T00:00:00+05:00", "ключ": key, "событие": event,
                             "id": "hr.stand", "сторож": key.split(":")[0], "почему": ""}},
                            ensure_ascii=False) + "\\n")
'''

# Дверь канала стенда: зов пишется в файл, доставка отклоняется — так
# недоставка в самотесте остаётся недоставкой.
STUB_DOOR = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> {calls!r}
exit {code}
'''


def build_tree(stand: Path, door_code: int) -> Path:
    """Дерево кода стенда в раскладке проекта: демон ищет config.sh как ../../scripts/lib."""
    tree = stand / "tree"
    (tree / "harness" / "demons").mkdir(parents=True)
    shutil.copy2(DEMON, tree / "harness" / "demons" / "heartbeat-watch.sh")
    # Библиотеки берутся рядом с пробой: так верно в обеих раскладках и при
    # подмене демона дверью HEARTBEAT_WATCH_SH.
    scripts = HERE
    shutil.copytree(scripts / "lib", tree / "scripts" / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(scripts / "language-level.py", tree / "scripts" / "language-level.py")
    # Судья выключателя: без него самотест получит rc 127 вместо ответа стенда.
    shutil.copy2(scripts / "run-background.sh", tree / "scripts" / "run-background.sh")
    writer = tree / "scripts" / "alert-task.py"
    writer.write_text(STUB_WRITER.format(stand=str(stand), calls=str(stand / "writer-calls.jsonl"),
                                         journal=JOURNAL), encoding="utf-8")
    door = tree / "scripts" / "tg_send.sh"
    door.write_text(STUB_DOOR.format(calls=str(stand / "door-calls.log"), code=door_code),
                    encoding="utf-8")
    for f in (writer, door):
        f.chmod(0o755)
    return tree


def passport(path: Path, tree: Path, log_dir: Path, hb_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'PROJECT_NAME="stand"\nPROJECT_DIR="{tree}"\nAGENT_USER="stand"\n'
        f'SECRETS_DIR="{path.parent}"\nTG_TOKEN_FILE="{path.parent}/no-token"\nTG_CHAT_ID=""\n'
        f'AUTONOMY="semi"\nHEARTBEAT_DIR="{hb_dir}"\nLOG_DIR="{log_dir}"\n'
        f'TG_CHANNEL_VARIANT="A"\n', encoding="utf-8")


def calls_of(path: Path) -> list:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def run(cmd: list, env: dict) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=TIMEOUT,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "таймаут"
    return p.returncode, p.stdout + p.stderr


def base_env(stand: Path) -> dict:
    """Окружение без боевых дверей: только то, что стенд задаёт сам."""
    home = stand / "home"
    home.mkdir(exist_ok=True)
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "LC_ALL": "C.UTF-8",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "PYTHONDONTWRITEBYTECODE": "1"}


def case_selftest_stays_in_stand(tmp: Path) -> None:
    print("— S1: самотест не зовёт писателя тревог и дверь канала дерева кода —")
    stand = tmp / "s1"
    stand.mkdir()
    tree = build_tree(stand, door_code=1)
    prod = stand / "prod"
    passport(prod / "install.conf", tree, prod / "log", prod / "hb")
    (prod / "harness.conf").write_text('HEARTBEAT_PERIODS="real:10"\n', encoding="utf-8")
    (prod / "log").mkdir()
    # Как gates.sh: PROJECT_DIR дерева в окружении; корень установки — «боевой» стенда.
    env = base_env(stand) | {"PROJECT_DIR": str(tree), "HARNESS_HOME": str(prod)}
    rc, out = run(["bash", str(tree / "harness" / "demons" / "heartbeat-watch.sh"), "--selftest"], env)
    check("S1·самотест на стенде проходит (стенд жив)", rc == 0 and "САМОТЕСТ ПРОЙДЕН" in out,
          f"rc={rc}; вывод: {out[-900:]}")

    writer = calls_of(stand / "writer-calls.jsonl")
    check("S1·писатель тревог дерева кода не позван самотестом", not writer,
          f"позван {len(writer)} раз: " + "; ".join(" ".join(c["argv"][:2]) + f" (паспорт {c['conf']})"
                                                     for c in writer))
    leaked = sorted(str(p.relative_to(prod)) for p in (prod / "log").rglob("*"))
    check("S1·«боевой» LOG_DIR стенда не тронут самотестом", not leaked, f"появились: {leaked}")
    door = stand / "door-calls.log"
    door_calls = door.read_text(encoding="utf-8").splitlines() if door.is_file() else []
    check("S1·дверь канала дерева кода не позвана самотестом (сводка-подделка не ушла бы владельцу)",
          not door_calls, f"позвана {len(door_calls)} раз: {[c[:80] for c in door_calls]}")


def case_resolve_names_outside_table(tmp: Path) -> None:
    print("— S2: прогон гасит открытые dead:* с именами вне таблицы —")
    stand = tmp / "s2"
    stand.mkdir()
    tree = build_tree(stand, door_code=0)
    log_dir, hb = stand / "log", stand / "hb"
    log_dir.mkdir()
    hb.mkdir()
    passport(stand / "install.conf", tree, log_dir, hb)
    (stand / "harness.conf").write_text('HEARTBEAT_PERIODS="alpha:10 beta:10"\n', encoding="utf-8")
    old = 1_700_000_000
    for name in (".install-stamp", "alpha", "beta"):
        (hb / name).touch()
    for name in (".install-stamp", "beta"):
        os.utime(hb / name, (old, old))
    opened = ["heartbeat-watch:dead:ghost", "heartbeat-watch:dead:beta", "other-watch:dead:ghost"]
    (log_dir / JOURNAL).write_text("".join(
        json.dumps({"ts": "2026-10-29T06:58:51+05:00", "ключ": k, "событие": "заведена",
                    "id": "hr.alert-" + k.replace(":", "-"), "сторож": k.split(":")[0], "почему": ""},
                   ensure_ascii=False) + "\n" for k in opened), encoding="utf-8")
    env = base_env(stand) | {"HARNESS_INSTALL_CONF": str(stand / "install.conf"),
                             "HARNESS_CONF": str(stand / "harness.conf")}
    rc, out = run(["bash", str(tree / "harness" / "demons" / "heartbeat-watch.sh")], env)
    calls = [c["argv"] for c in calls_of(stand / "writer-calls.jsonl")]
    resolved = {a[1] for a in calls if a[:1] == ["--resolve"]}
    raised = {a[1] for a in calls if a[:1] == ["--key"]}
    said = f"rc={rc}; зовы писателя: {calls}; вывод: {out[-400:]}"

    check("S2·стенд жив: живая alpha гасит свою тревогу", "heartbeat-watch:dead:alpha" in resolved, said)
    check("S2·мёртвая beta из таблицы заведена и не погашена",
          "heartbeat-watch:dead:beta" in raised and "heartbeat-watch:dead:beta" not in resolved, said)
    check("S2·открытая heartbeat-watch:dead:ghost (имени нет в HEARTBEAT_PERIODS) погашена",
          "heartbeat-watch:dead:ghost" in resolved, said)
    check("S2·тревога чужого сторожа other-watch:dead:ghost не тронута",
          "other-watch:dead:ghost" not in resolved, said)


def dead_line(out: str, name: str) -> bool:
    return any(line.startswith(f"• {name} — МЁРТВ") for line in out.splitlines())


def case_threshold_multiplier(tmp: Path) -> None:
    print("— S3: порог «мёртв» = период × множитель метки из файла множителей —")
    stand = tmp / "s3"
    stand.mkdir()
    tree = build_tree(stand, door_code=1)
    log_dir, hb = stand / "log", stand / "hb"
    log_dir.mkdir()
    hb.mkdir()
    passport(stand / "install.conf", tree, log_dir, hb)
    (stand / "harness.conf").write_text(
        'HEARTBEAT_PERIODS="backup:1440 nightly-gates:1440"\n', encoding="utf-8")
    mult = stand / "system-jobs.yaml"
    mult.write_text("запас-порога:\n  backup: 1\n  nightly-gates: 2\n", encoding="utf-8")
    env = base_env(stand) | {"HARNESS_INSTALL_CONF": str(stand / "install.conf"),
                             "HARNESS_CONF": str(stand / "harness.conf"),
                             "HEARTBEAT_MULT_FILE": str(mult), "HEARTBEAT_DRY_SEND": "1"}
    demon = ["bash", str(tree / "harness" / "demons" / "heartbeat-watch.sh")]

    # Возраст — минутами: «25 hours ago» touch понимает иначе, чем ждёшь.
    def age(name: str, minutes: int) -> None:
        subprocess.run(["touch", "-d", f"{minutes} minutes ago", str(hb / name)], check=True)

    age(".install-stamp", 10_000)
    age("backup", 1500)
    age("nightly-gates", 1500)
    rc, out = run(demon, env)
    said = f"rc={rc}; вывод: {out[-700:]}"
    check("S3·стенд жив: прогон прошёл, сводка в выводе", rc == 0 and "Утренняя сводка" in out, said)
    check("S3·backup 25 ч при периоде 24 ч (×1) — МЁРТВ", dead_line(out, "backup"), said)
    check("S3·nightly-gates 25 ч при периоде 24 ч (×2) — жив", rc == 0 and not dead_line(out, "nightly-gates"), said)
    # Стенд не дотянулся до боевого: тревоги и дверь не зовутся (DRY_SEND),
    # боевые каталоги в выводе не названы. Что метки читаются из стенда,
    # доказывают сами вердикты выше: возраст задан только меткам стенда.
    leaked = calls_of(stand / "writer-calls.jsonl") or (stand / "door-calls.log").exists()
    check("S3·стенд не вышел наружу: ни писателя тревог, ни двери, ни боевых каталогов",
          not leaked and "/var/log/harness" not in out and "/var/lib/harness" not in out, said)

    age("nightly-gates", 2940)
    rc, out = run(demon, env)
    check("S3·nightly-gates 49 ч при периоде 24 ч (×2) — МЁРТВ", dead_line(out, "nightly-gates"),
          f"rc={rc}; вывод: {out[-700:]}")

    # Файл множителей есть, но не читается: молча вернуть всем ×2 значило бы
    # растянуть тревогу о страховке на лишние сутки.
    mult.chmod(0)
    try:
        rc, out = run(demon, env)
    finally:
        mult.chmod(0o600)
    check("S3·нечитаемый файл множителей — громкий отказ (код ≠ 0, сводки нет)",
          rc != 0 and "Утренняя сводка" not in out, f"rc={rc}; вывод: {out[-700:]}")


def case_failing_last_run(tmp: Path) -> None:
    print("— S4: свежая метка, но последний запуск вышел кодом вне законных — падающий —")
    stand = tmp / "s4"
    stand.mkdir()
    tree = build_tree(stand, door_code=1)
    log_dir, hb = stand / "log", stand / "hb"
    log_dir.mkdir()
    hb.mkdir()
    passport(stand / "install.conf", tree, log_dir, hb)
    names = "crashy steady healed paused verdict wrongcode"
    (stand / "harness.conf").write_text(f'HEARTBEAT_PERIODS="{" ".join(n + ":10" for n in names.split())}"\n',
                                        encoding="utf-8")
    for name in (".install-stamp", *names.split()):
        (hb / name).touch()
    # Законные коды — ДАННЫЕ библиотеки: общие (пауза) и поле записи (вердикт демона).
    library = stand / "process-library.json"
    library.write_text(json.dumps({"общие_законные_коды_выхода": [78], "процессы": [
        {"id": "verdict", "законные_коды_выхода": [1]},
        {"id": "wrongcode", "законные_коды_выхода": [1]}]}, ensure_ascii=False), encoding="utf-8")

    def end(ident: str, rc: int) -> str:
        return json.dumps({"event": "process.end", "data": {"id": ident, "pid": 1, "rc": rc}}) + "\n"
    # healed: упал, потом вышел чисто — судится ПОСЛЕДНИЙ запуск.
    (log_dir / "process-runs.jsonl").write_text(
        end("crashy", 1) + end("steady", 0) + end("healed", 1) + end("healed", 0)
        + end("paused", 78) + end("verdict", 1) + end("wrongcode", 2), encoding="utf-8")
    env = base_env(stand) | {"HARNESS_INSTALL_CONF": str(stand / "install.conf"),
                             "HARNESS_CONF": str(stand / "harness.conf"), "HEARTBEAT_DRY_SEND": "1",
                             "HARNESS_PROCESS_LIBRARY_JSON": str(library)}
    rc, out = run(["bash", str(tree / "harness" / "demons" / "heartbeat-watch.sh")], env)
    said = f"rc={rc}; вывод: {out[-900:]}"

    def falls(name: str) -> bool:
        return any(line.startswith(f"• {name} — ПАДАЕТ") for line in out.splitlines())
    check("S4·crashy (rc=1, кода в библиотеке нет) — падающий, назван в сводке", falls("crashy"), said)
    check("S4·wrongcode (rc=2, законен только 1) — падающий", falls("wrongcode"), said)
    check("S4·steady (rc=0) не падающий", not falls("steady"), said)
    check("S4·healed (упал, затем rc=0) не падающий", not falls("healed"), said)
    check("S4·paused (rc=78, общий законный код) не падающий", not falls("paused"), said)
    check("S4·verdict (rc=1, законен по полю записи) не падающий", not falls("verdict"), said)
    check("S4·сводка: МЁРТВЫХ 2, живых 4", "живых 4, МЁРТВЫХ 2" in out, said)


def main() -> int:
    print(f"демон: {DEMON}")
    if not DEMON.is_file():
        print(f"ПЛОХО · демона нет: {DEMON}\nитого путей 0, неудач 1")
        return 1
    with tempfile.TemporaryDirectory(prefix="heartbeat-alerts-") as tmp:
        os.chmod(tmp, 0o700)
        tmp_path = Path(tmp).resolve()
        for case in (case_selftest_stays_in_stand, case_resolve_names_outside_table,
                     case_threshold_multiplier, case_failing_last_run):
            try:
                case(tmp_path)
            except Exception as exc:  # noqa: BLE001 — упавший стенд — неудача пробы, а не зелёный
                check(f"{case.__name__}: стенд собран", False, f"{type(exc).__name__}: {exc}")
    print(f"итого путей {paths}, неудач {failures}")
    return 0 if failures == 0 and paths > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
