#!/usr/bin/env python3
"""Пробы запроса агента к журналам: scripts/events.py status|task|journals|lost.

Зачем. Главный читатель журналов — агент (слово владельца 27.10), и он
платит за каждый знак ответа в КАЖДОМ следующем шаге сессии. Сегодня вопрос
«сколько отказов за неделю» сырым grep стоит ≈72 500 токенов, «что было с
задачей» дёшев только потому, что слеп: задачу пишут 3 журнала из 35.
Спека docs/specs/2026-10-27-логирование-харнеса.md §7, критерии 7 и 14(а).
Проба охраняет четыре обещания:
  1. ответ укладывается в бюджет команды (status 150, task 400, journals 300,
     lost 200 токенов — оценка той же функцией, что у гигиены контекста);
  2. в ответе ФАКТ, который знает только стенд (число потерь, имя журнала с
     битой строкой, отказы по каждому из трёх путей, ≥ 4 src задачи) — пустой
     или чужой ответ не проходит;
  3. непустой ответ кончается строкой «дальше:», урезанный — называет
     «срезано:»; пустой называет, сколько просмотрено, и подсказки не выдумывает;
  4. потеря записи видна первой командой смены и гасится только разбором (--ack);
  5. CLI работает у обоих пользователей группы, кто бы ни собрал сводку первым;
     id задач карты (и кириллические) принимаются, подсказка исполнима;
     шаг установки `events.py install` приводит .events и права журналов.
«НЕ СУДИТСЯ» (второго пользователя нет, CLI ему не читается) — не зелёный
путь, число печатается в итоге.

Стенд — синтетические журналы в СТАРЫХ форматах боевых писателей (ключи сняты
с боевых файлов 27.10, значения выдуманы) плюс строки v1 и две строки потерь.
Механизм зовётся с дверями HARNESS_LOG_DIR / HARNESS_EVENTS_STATE /
HARNESS_DEVMAP; путь к CLI — дверь HARNESS_EVENTS_CLI. Механизм боевых журналов
не видит: в стенде 6 отказов, в боевом guard.jsonl — больше 1600; ответ с
боевым числом выдал бы утечку двери. Второй стенд — КОПИЯ боевых журналов
(проба только читает их для копирования, каталог копии 0700, удаляется в
конце): бюджет обязан держаться на боевом объёме, а не на десяти строках.
Снимок /var/log/harness/.events до и после.

    python3 scripts/test_events_cli.py
Код возврата: 0 — все пути прошли; 1 — есть упавший или судить было нечего.
"""
from __future__ import annotations

import datetime as dt
import grp
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = os.environ.get("HARNESS_EVENTS_CLI") or str(ROOT / "scripts" / "events.py")
LIVE_EVENTS = Path("/var/log/harness/.events")
TASK = "hr.harness-detailed-logging"
BUDGET = {"status": 150, "task": 400, "journals": 300, "lost": 200}
SRC_GUARD = "scripts/hooks/guard_bash.py"

paths = failures = not_judged = 0


def check(name: str, expected, actual) -> None:
    global paths, failures
    paths += 1
    if actual == expected:
        print(f"ок  · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name}: ждали «{expected}», вышло «{str(actual)[:400]}»")


def skip(name: str, why: str) -> None:
    """Случай не судился — это НЕ зелёный, он называется в итоге отдельно."""
    global not_judged
    not_judged += 1
    print(f"НЕ СУДИТСЯ · {name}: {why}")


def events_group() -> tuple[str, int]:
    """Группа событий стенда: harness-panel, если агент в ней, иначе своя."""
    try:
        g = grp.getgrnam("harness-panel")
        if g.gr_gid in os.getgroups():
            return g.gr_name, g.gr_gid
    except KeyError:
        pass
    return grp.getgrgid(os.getgid()).gr_name, os.getgid()


def load_tokens():
    """Одна дверь оценки токенов: после Ф1 — scripts/lib/token_estimate.py,
    до неё — та же формула в гигиене контекста (§7.2). Своей копии проба не
    держит: иначе бюджет судился бы не той линейкой, что у механизма."""
    lib = ROOT / "scripts" / "lib" / "token_estimate.py"
    if lib.exists():
        spec = importlib.util.spec_from_file_location("token_estimate", lib)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.tokens
    spec = importlib.util.spec_from_file_location("hygiene", ROOT / "scripts" / "check-context-hygiene.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.токенов


tokens = load_tokens()
NOW = dt.datetime.now().astimezone()


def local(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def zulu(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def naive(hours_ago: float) -> str:
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def v1(hours_ago, src, event, outcome, task=None, task_from="none", data=None) -> dict:
    return {"v": 1, "ts": local(hours_ago), "src": src, "event": event, "outcome": outcome,
            "task": task, "task_from": task_from, "session": None, "data": data or {}}


def write_jsonl(path: Path, rows: list, extra_raw: list[str] = ()) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + "".join(x + "\n" for x in extra_raw)
    path.write_text(text, encoding="utf-8")


def make_devmap(path: Path) -> None:
    text = (ROOT / "dev-map.yaml").read_text(encoding="utf-8")
    text = re.sub(r"(\n\s+status: )wip\b", r"\1test", text)
    text, n = re.subn(r"(- id: " + re.escape(TASK) + r"\n\s+status: )\w+", r"\1wip", text, count=1)
    if n != 1:
        raise SystemExit(f"проба: карточка {TASK} не найдена в dev-map.yaml")
    path.write_text(text, encoding="utf-8")


def build_stand(root: Path) -> dict:
    """Журналы стенда и НЕЗАВИСИМЫЙ счёт отказов по сырым полям (крит. 7):
    guard verdict=="deny", v1 outcome=="refused", gates verdict=="fail"
    (значение outcome_map, выписанное литералом). Счёт каждого пути > 0 —
    иначе два нуля сошлись бы без работы."""
    log = root / "log"
    state = log / ".events"
    state.mkdir(parents=True)
    guard = [{"ts": local(h), "verdict": "deny", "rule": "compose restart", "command": "docker compose restart x"}
             for h in (1, 2, 3)]
    guard += [{"ts": local(4), "verdict": "followup", "after_rule": "x", "outcome": "отступил", "overlap": 0.0}] * 2
    guard.append(v1(0.5, SRC_GUARD, "guard.refused", "refused", TASK, "arg", {"verdict": "deny"}))
    write_jsonl(log / "guard.jsonl", guard)
    gates = [{"ts": local(h), "gate": "pre-commit", "head": "abc1234", "staged": 3, "verdict": v, "failed": f}
             for h, v, f in ((5, "pass", ""), (6, "pass", ""), (7, "fail", "check-secrets"))]
    gates.append(v1(0.6, "scripts/pre-commit-gate.sh", "gate.verdict", "refused", TASK, "map"))
    write_jsonl(log / "gates.jsonl", gates)
    write_jsonl(log / "skills.jsonl", [
        {"ts": zulu(8), "skill": "plan-writing", "task": TASK, "откуда_задача": "карта", "источник": "хук",
         "успех": True, "сессия": "s-stand"},
        v1(0.7, "scripts/hooks/skill_log.sh", "skill.called", "ok", TASK, "map", {"skill": "tests-before-code"}),
    ])
    write_jsonl(log / "tg_out.jsonl", [
        {"ts": local(9), "uroven": "простой", "iz_sessii": True, "text": "статус стенда"},
        v1(0.8, "scripts/tg_send.sh", "tg.sent", "held", TASK, "arg", {"why": "лимит"}),
    ])
    write_jsonl(log / "rotation.jsonl", [
        {"ts": local(10), "event": "silence_kick", "session": "s-stand", "detail": "тишина 19 мин"},
        v1(0.9, "harness/demons/session-warden.sh", "rotation.kick", "ok", TASK, "history"),
    ], extra_raw=["{битая строка стенда"])
    write_jsonl(log / "срок-входа.jsonl", [
        {"ts": naive(11), "доступ_до": naive(-2), "ключ_до": naive(-500)}])
    write_jsonl(state / "lost.jsonl", [
        {"ts": local(0.3), "journal": "guard", "src": SRC_GUARD, "reason": "lock-timeout", "kind": "lost"},
        {"ts": local(0.2), "journal": "gates", "src": "scripts/pre-commit-gate.sh", "reason": "short-write",
         "kind": "lost"},
    ])
    make_devmap(root / "dev-map.yaml")
    by_path = {
        "guard deny": sum(1 for r in guard if r.get("verdict") == "deny" and "v" not in r),
        "v1 refused": sum(1 for f in ("guard", "gates", "skills", "tg_out", "rotation")
                          for line in (log / f"{f}.jsonl").read_text(encoding="utf-8").splitlines()
                          if line.startswith('{"v": 1') and json.loads(line).get("outcome") == "refused"),
        "gates fail (outcome_map)": sum(1 for r in gates if r.get("verdict") == "fail" and "v" not in r),
    }
    return {"log": log, "state": state, "devmap": root / "dev-map.yaml", "refusals": by_path}


def copy_live_stand(root: Path) -> dict:
    """Копия боевых журналов (только чтение боевых; CLI потом видит ТОЛЬКО
    копию) плюс две подложенные строки потерь — факт, который знает лишь стенд.
    Симлинк tokens.jsonl не копируется: он второй раз тот же месяц."""
    src = Path("/var/log/harness")
    log = root / "log"
    state = log / ".events"
    state.mkdir(parents=True)
    os.chmod(root, 0o700)
    for p in list(src.glob("*.jsonl")) + list(src.glob("evo/*.jsonl")):
        if p.is_symlink() or not os.access(p, os.R_OK):
            continue
        dst = log / p.relative_to(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dst)
    write_jsonl(state / "lost.jsonl", [
        {"ts": local(0.3), "journal": "guard", "src": SRC_GUARD, "reason": "lock-timeout", "kind": "lost"},
        {"ts": local(0.2), "journal": "gates", "src": "scripts/pre-commit-gate.sh", "reason": "short-write",
         "kind": "lost"},
    ])
    make_devmap(root / "dev-map.yaml")
    return {"log": log, "state": state, "devmap": root / "dev-map.yaml"}


def run(stand: dict, *args: str, extra: dict | None = None, as_user: str | None = None,
        umask: int | None = None):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "HARNESS_LOG_DIR": str(stand["log"]),
        "LOG_DIR": str(stand["log"]),
        "HARNESS_EVENTS_STATE": str(stand["state"]),
        "HARNESS_DEVMAP": str(stand["devmap"]),
        "PANEL_STATE_DIR": str(stand["log"].parent / "panel-state"),
    }
    env.update(extra or {})
    cmd = [sys.executable, CLI, *args]
    if as_user:
        # sudo ставит свой umask (у harness-panel файл выходил 0660 и без fchmod):
        # umask вызова доносится до ребёнка сам, через sh.
        if umask is not None:
            cmd = ["sh", "-c", f'umask {umask:04o}; exec "$@"', "sh"] + cmd
        cmd = ["sudo", "-n", "-u", as_user, "env"] + [f"{k}={v}" for k, v in env.items()] + cmd
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120, cwd="/",
                       umask=-1 if umask is None else umask)
    return p.returncode, p.stdout, p.stderr


def last_line(text: str) -> str:
    lines = [x for x in text.splitlines() if x.strip()]
    return lines[-1] if lines else ""


def first_line(text: str) -> str:
    lines = [x for x in text.splitlines() if x.strip()]
    return lines[0] if lines else ""


def answer_shape(name: str, cmd: str, rc: int, out: str, err: str) -> None:
    """Общее для каждого непустого ответа: rc 0, бюджет, «дальше:» последней строкой."""
    check(f"{name}: rc 0", 0, rc)
    if rc != 0:
        print(f"     stderr: {err.strip()[:300]}")
    n = tokens(out)
    check(f"{name}: в бюджете {BUDGET[cmd]} токенов (вышло {n})", True, 0 < n <= BUDGET[cmd])
    check(f"{name}: последняя строка — «дальше:»", True, last_line(out).startswith("дальше:"))


def line_with(text: str, *needles: str) -> str:
    for x in text.splitlines():
        if all(n in x for n in needles):
            return x
    return ""


def live_snapshot():
    if not LIVE_EVENTS.exists():
        return ("нет",)
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in LIVE_EVENTS.rglob("*")))


def live_modes() -> dict:
    """Права боевых журналов корня: шаг установки на стенде не должен их задеть."""
    out = {}
    for p in Path("/var/log/harness").glob("*.jsonl"):
        if not p.is_symlink():
            s = p.stat()
            out[p.name] = (stat.S_IMODE(s.st_mode), s.st_gid)
    return out


def devmap_cyrillic_ids() -> list[str]:
    """Id задач с кириллицей — РАЗБОРОМ настоящей карты, не списком в пробе:
    список, переписанный сюда, перестал бы видеть новые карточки."""
    import yaml
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str) and re.search(r"[а-яёА-ЯЁ]", node["id"]):
                found.append(node["id"])
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(yaml.safe_load((ROOT / "dev-map.yaml").read_text(encoding="utf-8")))
    return found


def hint_commands(out: str) -> list[str]:
    line = next((x for x in out.splitlines() if x.startswith("дальше:")), "")
    return [c.strip() for c in line[len("дальше:"):].split(";") if c.strip().startswith("events.py")]


def case_cyrillic_task(base: Path) -> None:
    print("── БОЛЬНОЙ (ревью п.4): task принимает кириллические id карты; подсказка refusals --by task исполнима ──")
    ids = devmap_cyrillic_ids()
    print(f"     кириллических id в dev-map.yaml: {len(ids)}")
    check("проба: кириллических id в карте больше нуля (пустой набор — суда нет)", True, len(ids) > 0)
    if not ids:
        return
    root = base / "cyr"
    log, state = root / "log", root / "log" / ".events"
    state.mkdir(parents=True)
    make_devmap(root / "dev-map.yaml")
    task_id = ids[0]
    write_jsonl(log / "guard.jsonl", [
        v1(1, SRC_GUARD, "guard.refused", "refused", task_id, "arg", {"verdict": "deny", "rule": "x"}),
        v1(2, SRC_GUARD, "guard.refused", "refused", task_id, "arg", {"verdict": "deny", "rule": "x"})])
    stand = {"log": log, "state": state, "devmap": root / "dev-map.yaml"}
    refused_ids = []
    for tid in ids:
        rc, out, err = run(stand, "task", tid)
        if rc != 0:
            refused_ids.append(f"{tid} rc {rc}: {first_line(err)[:80]}")
    check(f"task <кириллический id>: отказов 0 из {len(ids)}", [], refused_ids)
    rc, out, err = run(stand, "task", task_id)
    check(f"task {task_id}: ответ с событиями стенда, не «событий нет»", (0, True, False),
          (rc, task_id in first_line(out), "событий нет" in out))
    rc, out, err = run(stand, "refusals", "--since", "7d", "--by", "task")
    check("refusals --by task: rc 0 и id задачи назван", (0, True), (rc, task_id in out))
    hints = hint_commands(out)
    check("refusals --by task: подсказка «дальше:» есть", True, bool(hints))
    bad = []
    for h in hints:
        argv = shlex.split(h)[1:]
        hrc, hout, herr = run(stand, *argv)
        if hrc != 0:
            bad.append(f"{h} → rc {hrc}: {first_line(herr)[:80]}")
    check("refusals --by task: каждая подсказанная команда исполнима (rc 0)", [], bad)


def case_index_lock_shared(base: Path) -> None:
    print("── БОЛЬНОЙ (ревью п.2): сводку собрал один пользователь группы — CLI работает и у другого ──")
    gname, gid = events_group()
    if gname != "harness-panel" or subprocess.run(
            ["sudo", "-n", "-u", "harness-panel", "true"], capture_output=True).returncode != 0:
        skip("CLI у второго пользователя", "второго пользователя нет (группа harness-panel или sudo -n)")
        return
    lib = Path(CLI).resolve().parent / "lib" / "events.py"
    for f in (CLI, str(lib)):
        if subprocess.run(["sudo", "-n", "-u", "harness-panel", "test", "-r", f], capture_output=True).returncode:
            skip("CLI у второго пользователя", f"{f} не читается harness-panel (каталог копии закрыт)")
            return
    os.chmod(base, 0o755)
    extra = {"EVENTS_GROUP": gname}
    for first, second in (("agent", "harness-panel"), ("harness-panel", "agent")):
        root = base / f"shared-{first}"
        stand = build_stand(root)
        os.chmod(root, 0o755)
        os.chown(stand["state"], -1, gid)
        os.chmod(stand["state"], 0o2770)  # так .events ставит шаг установки (§4.2)
        rc, out, err = run(stand, "index", extra=extra, umask=0o022,
                           as_user=None if first == "agent" else "harness-panel")
        check(f"сводку первым собрал {first}: rc 0", 0, rc)
        lock = stand["state"] / "index" / ".lock"
        check(f"{first} создал index/.lock 0660", oct(0o660),
              oct(stat.S_IMODE(lock.stat().st_mode)) if lock.exists() else None)
        rc, out, err = run(stand, "status", extra=extra, umask=0o022,
                           as_user=None if second == "agent" else "harness-panel")
        check(f"после {first} status у {second}: rc 0, без PermissionError", (0, False),
              (rc, "PermissionError" in err))
        if rc:
            print(f"     stderr: {err.strip()[:300]}")


INSTALL_CMD = "install"


def git_commit(repo: Path, message: str, branch: str | None = None) -> str:
    """Коммит стенда без правок дерева; вернуть полный sha."""
    base_cmd = ["git", "-C", str(repo), "-c", "user.name=probe", "-c", "user.email=probe@stand",
                "-c", "commit.gpgsign=false"]
    if branch:
        subprocess.run(base_cmd + ["checkout", "-q", "-B", branch], check=True, capture_output=True)
    subprocess.run(base_cmd + ["commit", "-q", "--allow-empty", "-m", message], check=True, capture_output=True)
    return subprocess.run(base_cmd + ["rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def tokens_row(hours_ago: float, session: str, n_in: int, out: int = 0, cache_w: int = 0,
               cache_r: int = 0) -> dict:
    return {"ts": (NOW - dt.timedelta(hours=hours_ago)).astimezone(dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.000Z"), "session": session, "model": "m", "in": n_in,
            "out": out, "cache_w": cache_w, "cache_r": cache_r, "tools": 0}


def json_counts(stand: dict, task_id: str) -> dict:
    rc, out, err = run(stand, "task", task_id, "--json")
    try:
        return (json.loads(out).get("counts") or {}) if rc == 0 else {"rc": rc, "err": err.strip()[:200]}
    except ValueError:
        return {"не JSON": out[:200]}


def case_inferred_task(base: Path) -> None:
    """Вывод задачи читалкой (спека §5.2, §7.1): tokens-* и rotation — по сессии
    («последний вызов скила с задачей в той же сессии ДО события»), gates — по
    коммиту (head до коммита → трейлер Dev-Map: ребёнка). Догадка помечена
    «приписано», в факты не идёт; расход: факт и приписано раздельно.
    Стенд знает ответ числом: 323 токена = 100+20+3 + 200 (чтение кэша не в счёт,
    строка ДО вызова скила — 1000 — не в счёт, чужая сессия — не в счёт)."""
    print("── БОЛЬНОЙ (Ф1б): задача выводится по сессии и коммиту, расход приписан числом ──")
    task_i, later, other = "hr.stand-inferred", "hr.stand-later", "hr.stand-other"
    amb_one, amb_two = "hr.stand-amb-one", "hr.stand-amb-two"
    root = base / "inferred"
    repo, log = root / "repo", root / "log"
    state = log / ".events"
    state.mkdir(parents=True)
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True, capture_output=True)
    c0 = git_commit(repo, "корень стенда")
    c_a = git_commit(repo, f"шаг А\n\nDev-Map: {amb_one}\nCo-Authored-By: probe <p@s>")
    git_commit(repo, f"шаг В\n\nDev-Map: {task_i}\nCo-Authored-By: probe <p@s>")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-B", "side", c0], check=True, capture_output=True)
    git_commit(repo, f"шаг Б\n\nDev-Map: {amb_two}")
    make_devmap(repo / "dev-map.yaml")
    stand = {"log": log, "state": state, "devmap": repo / "dev-map.yaml"}
    write_jsonl(log / "skills.jsonl", [
        {"ts": zulu(5), "skill": "plan-writing", "task": task_i, "откуда_задача": "карта", "источник": "хук",
         "успех": True, "сессия": "s-one"},
        {"ts": zulu(1), "skill": "task-closing", "task": later, "откуда_задача": "карта", "источник": "хук",
         "успех": True, "сессия": "s-one"},
        {"ts": zulu(5), "skill": "plan-writing", "task": other, "откуда_задача": "карта", "источник": "хук",
         "успех": True, "сессия": "s-two"},
    ])
    write_jsonl(log / "tokens-2026-10.jsonl", [
        tokens_row(6, "s-one", 1000),
        tokens_row(4, "s-one", 100, out=20, cache_w=3, cache_r=50000),
        tokens_row(3, "s-one", 200, cache_r=7),
        tokens_row(0.5, "s-one", 7),
        tokens_row(4, "s-two", 9),
        tokens_row(4, "s-nobody", 11),
    ])
    write_jsonl(log / "rotation.jsonl", [
        {"ts": local(2), "event": "silence_kick", "session": "s-one", "detail": "тишина стенда"}])
    write_jsonl(log / "gates.jsonl", [
        {"ts": local(3), "gate": "pre-commit", "head": c_a[:8], "staged": 2, "verdict": "fail",
         "failed": "check-secrets"},
        {"ts": local(2.5), "gate": "pre-commit", "head": c0[:8], "staged": 1, "verdict": "pass", "failed": ""},
    ])
    write_jsonl(log / "guard.jsonl", [
        v1(1.5, SRC_GUARD, "guard.refused", "refused", task_i, "arg", {"verdict": "deny", "rule": "x"})])

    got = json_counts(stand, task_i)
    check(f"task {task_i} --json: фактов 2 (skills карта, guard arg), догадки в факты не идут", 2, got.get("facts"))
    check(f"task {task_i} --json: приписано 4 (2 tokens по сессии, rotation по сессии, gates по коммиту)",
          4, got.get("guesses"))
    check(f"task {task_i} --json: расход приписан 323 (in+out+cache_w, только после вызова скила)",
          323, got.get("spent_guess"))
    check(f"task {task_i} --json: расход фактом 0", 0, got.get("spent_fact"))
    check(f"task {later} --json: расход строки после второго вызова скила — 7", 7,
          json_counts(stand, later).get("spent_guess"))
    check(f"task {other} --json: чужая сессия — своя задача, 9", 9, json_counts(stand, other).get("spent_guess"))
    amb = (json_counts(stand, amb_one).get("events", 0), json_counts(stand, amb_two).get("events", 0))
    check("gates на head с двумя детьми разных задач — не выводится ни в одну", (0, 0), amb)

    rc, out, err = run(stand, "task", task_i)
    answer_shape(f"task {task_i}", "task", rc, out, err)
    guess_line = "\n".join(x for x in out.splitlines() if "приписано" in x)
    for short in ("tokens-collector", "session-warden", "pre-commit-gate"):
        check(f"task {task_i}: «приписано» называет {short}", True, short in guess_line)
    check(f"task {task_i}: строка расхода с приписанным 323", True,
          bool(re.search(r"(?<!\d)323(?!\d)", line_with(out, "расход"))))


def case_install(base: Path) -> None:
    print(f"── БОЛЬНОЙ (ревью п.3, контракт крит. 24, edge 49): events.py {INSTALL_CMD} приводит .events и права журналов ──")
    gname, gid = events_group()
    root = base / "install"
    log = root / "log"
    (log / "evo").mkdir(parents=True)
    # Копии боевых журналов с их НАСТОЯЩИМИ правами: по одному на каждый режим,
    # что встречается на машине (сегодня 644, 664, 600) — шаг судится на том, что есть.
    src = Path("/var/log/harness")
    picked: dict[int, Path] = {}
    for p in sorted(src.glob("*.jsonl")):
        if p.is_symlink() or not os.access(p, os.R_OK) or p.stat().st_size > 2_000_000:
            continue
        picked.setdefault(stat.S_IMODE(p.stat().st_mode), p)
    for mode, p in picked.items():
        shutil.copyfile(p, log / p.name)
        os.chmod(log / p.name, mode)
    nested = log / "evo" / "summary.jsonl"
    nested.write_text('{"ts": "2026-10-27T14:05:02+05:00"}\n', encoding="utf-8")
    os.chmod(nested, 0o644)
    (log / ".events").mkdir()
    os.chmod(log / ".events", 0o755)  # каталог есть, но не тот: без setgid, группа своя, index/runs нет
    print(f"     копий журналов: {len(picked)}, режимы {sorted(oct(m) for m in picked)}")
    check("проба: копий журналов больше нуля и среди них не только 0640", True,
          bool(picked) and any(m != 0o640 for m in picked))
    stand = {"log": log, "state": log / ".events", "devmap": root / "dev-map.yaml"}
    make_devmap(stand["devmap"])
    extra = {"EVENTS_GROUP": gname}

    def judge(label: str) -> None:
        for sub in ("", "index", "runs"):
            d = log / ".events" / sub
            got = (oct(stat.S_IMODE(d.stat().st_mode)), d.stat().st_gid) if d.is_dir() else None
            check(f"{label}: .events/{sub} 2770 группы {gname}", (oct(0o2770), gid), got)
        wrong = []
        for p in sorted(log.glob("*.jsonl")):
            s = p.stat()
            if (stat.S_IMODE(s.st_mode), s.st_gid) != (0o640, gid):
                wrong.append(f"{p.name} {oct(stat.S_IMODE(s.st_mode))} gid {s.st_gid}")
        check(f"{label}: журналы корня 0640 группы {gname}", [], wrong)
        check(f"{label}: журнал во вложенном каталоге не тронут (только корень)", oct(0o644),
              oct(stat.S_IMODE(nested.stat().st_mode)))

    rc, out, err = run(stand, INSTALL_CMD, extra=extra, umask=0o022)
    check(f"{INSTALL_CMD}: rc 0", 0, rc)
    if rc:
        print(f"     stderr: {err.strip()[:300]}")
    judge("первый запуск")
    rc, out, err = run(stand, INSTALL_CMD, extra=extra, umask=0o022)
    check(f"{INSTALL_CMD} повторно: rc 0 (идемпотентен)", 0, rc)
    judge("повтор")

    import yaml
    jobs = yaml.safe_load((ROOT / "harness" / "config" / "system-jobs.yaml").read_text(encoding="utf-8")) or {}
    commands = [c for step in (jobs.get("живые_правки") or []) for c in (step.get("команды") or [])]
    check(f"system-jobs.yaml: живая правка зовёт scripts/events.py {INSTALL_CMD}", True,
          any(re.search(rf"scripts/events\.py\s+{INSTALL_CMD}\b", c) for c in commands))


def main() -> int:
    snap = live_snapshot()
    modes_before = live_modes()
    base = Path(tempfile.mkdtemp(prefix="events-cli-probe-"))
    print(f"стенд: {base}; механизм: {CLI}")
    try:
        for case in (case_cyrillic_task, case_index_lock_shared, case_install, case_inferred_task):
            try:
                case(base)
            except Exception as exc:  # падение случая — его неудача, а не конец прогона
                check(f"{case.__name__}: случай доработал до конца", "без исключения", f"{type(exc).__name__}: {exc}")
        stand = build_stand(base / "full")
        refusals = stand["refusals"]
        total = sum(refusals.values())
        print(f"независимый счёт отказов стенда: {refusals} = {total}")
        check("проба: отказы по каждому из трёх путей > 0 (иначе нули сошлись бы без работы)",
              True, all(v > 0 for v in refusals.values()))

        print("── БОЛЬНОЙ: status — потеря видна первой командой смены ──")
        rc, out, err = run(stand, "status")
        answer_shape("status", "status", rc, out, err)
        check("status: вердикт первой строкой — «не в норме»", True, "не в норме" in first_line(out))
        check("status: строка потерь с числом 2", True, bool(re.search(r"(?<!\d)2(?!\d)", line_with(out, "потер"))))
        check(f"status: отказов {total} — счёт стенда, не боевой", True,
              bool(re.search(rf"(?<!\d){total}(?!\d)", line_with(out, "отказ"))))

        print("── status --json: машине — фиксированный набор ключей ──")
        rc, out, err = run(stand, "status", "--json")
        try:
            obj = json.loads(out)
        except ValueError:
            obj = None
        check("status --json: один объект JSON", True, isinstance(obj, dict))
        check("status --json: ключи verdict, counts, items, truncated, next", True,
              isinstance(obj, dict) and {"verdict", "counts", "items", "truncated", "next"} <= set(obj))

        print("── task: что было с задачей — факты от ≥ 4 механизмов, догадка помечена ──")
        rc, out, err = run(stand, "task", TASK)
        answer_shape("task", "task", rc, out, err)
        for short in ("guard_bash", "pre-commit-gate", "skill_log", "tg_send"):
            check(f"task: назван механизм {short}", True, short in out)
        check("task: догадка (history) помечена «приписано»", True, "приписано" in out)
        check("task: полный путь src не тратит бюджет (короткое имя)", False, "scripts/hooks/guard_bash.py" in out)

        print("── БОЛЬНОЙ: --бюджет режет ответ честно — «срезано:», а не молча ──")
        rc, out, err = run(stand, "task", TASK, "--бюджет", "60")
        check("task --бюджет 60: rc 0", 0, rc)
        check(f"task --бюджет 60: ≤ 60 токенов (вышло {tokens(out)})", True, 0 < tokens(out) <= 60)
        check("task --бюджет 60: урезанное названо «срезано:»", True, "срезано:" in out)

        print("── крит. 7: refusals сходится с независимым счётом по сырым полям ──")
        rc, out, err = run(stand, "refusals", "--since", "7d", "--by", "src")
        check("refusals: rc 0", 0, rc)
        check(f"refusals: итог {total} == независимому счёту", True,
              bool(re.search(rf"(?<!\d){total}(?!\d)", line_with(out, "итог"))))
        check("refusals: последняя строка — «дальше:»", True, last_line(out).startswith("дальше:"))

        print("── БОЛЬНОЙ: journals — битая строка названа журналом и числом ──")
        rc, out, err = run(stand, "journals")
        answer_shape("journals", "journals", rc, out, err)
        check("journals: rotation и «битых … 1» в одной строке", True,
              bool(re.search(r"(?<!\d)1(?!\d)", line_with(out, "rotation", "бит"))))

        print("── БОЛЬНОЙ: lost — обе причины; --ack гасит, а не стирает ──")
        rc, out, err = run(stand, "lost")
        answer_shape("lost", "lost", rc, out, err)
        check("lost: причина lock-timeout", True, "lock-timeout" in out)
        check("lost: причина short-write", True, "short-write" in out)
        before = (stand["state"] / "lost.jsonl").read_text(encoding="utf-8")
        rc, out, err = run(stand, "lost", "--ack", "разобрано пробой")
        check("lost --ack: rc 0", 0, rc)
        after = (stand["state"] / "lost.jsonl").read_text(encoding="utf-8")
        check("lost --ack: прежние строки потерь не стёрты (дозапись)", True, after.startswith(before))
        rc, out, err = run(stand, "lost")
        check("lost после ack: причин без разбора нет", (0, False, False),
              (rc, "lock-timeout" in out, "short-write" in out))
        rc, out, err = run(stand, "status")
        check("status после ack: строки потерь с числом 2 нет", "",
              line_with(out, "потер") if re.search(r"(?<!\d)2(?!\d)", line_with(out, "потер")) else "")

        print("── пустой стенд: «нет событий» отличимо от «не прочитал» ──")
        empty_root = base / "empty"
        (empty_root / "log" / ".events").mkdir(parents=True)
        make_devmap(empty_root / "dev-map.yaml")
        empty = {"log": empty_root / "log", "state": empty_root / "log" / ".events",
                 "devmap": empty_root / "dev-map.yaml"}
        rc, out, err = run(empty, "status")
        check("пустой status: rc 0", 0, rc)
        check("пустой status: «всё в норме» первой строкой", True, "всё в норме" in first_line(out))
        check("пустой status: назван счёт просмотренного", True, "просмотрено" in out)
        rc, out, err = run(stand, "task", "hr.no-such-task-in-stand")
        check("неизвестная задача: rc 0", 0, rc)
        check("неизвестная задача: «просмотрено N журналов, M строк»", True, "просмотрено" in out)
        check("неизвестная задача: подсказки не выдумано", False, "дальше:" in out)

        print("── БОЛЬНОЙ: боевой объём — бюджет держится на копии всех журналов (крит. 14а в объёме Ф1) ──")
        copy = copy_live_stand(base / "copy")
        mb = sum(p.stat().st_size for p in copy["log"].rglob("*.jsonl")) / 1e6
        print(f"     копия: {mb:.1f} МБ")
        check("копия не меньше 20 МБ (иначе это не боевой объём)", True, mb >= 20)
        spent = 0
        for cmd in ("status", "journals", "lost"):
            rc, out, err = run(copy, cmd)
            answer_shape(f"копия {cmd}", cmd, rc, out, err)
            spent += tokens(out)
            if cmd == "status":
                check("копия status: строка потерь с числом 2 (подложено стендом)", True,
                      bool(re.search(r"(?<!\d)2(?!\d)", line_with(out, "потер"))))
            if cmd == "lost":
                check("копия lost: обе подложенные причины", (True, True),
                      ("lock-timeout" in out, "short-write" in out))
        print(f"     цена трёх ответов на копии: {spent} токенов")

        print("── неверный вызов: rc 2 с причиной ──")
        # rc 2 даёт и сам python на несуществующий файл скрипта: голый код
        # возврата был зелен без CLI вовсе (красный прогон 27.10). Судим ПРИЧИНУ:
        # отказ CLI называет свои команды, отказ python — «No such file».
        rc, out, err = run(stand, "нет-такой-команды")
        check("неизвестная команда: rc 2 и список команд в отказе", (2, True, False),
              (rc, "status" in out + err and "lost" in out + err, "No such file" in err))
        rc, out, err = run(stand, "task")
        check("task без id: rc 2 и отказ называет task", (2, True, False),
              (rc, "task" in out + err, "No such file" in err))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    check("боевой /var/log/harness/.events не тронут пробой", snap, live_snapshot())
    modes_after = live_modes()
    check("права боевых журналов не тронуты шагом установки на стенде", [],
          sorted(n for n in modes_before.keys() & modes_after.keys() if modes_before[n] != modes_after[n]))
    print(f"\nитого путей {paths}, неудач {failures}, не судится {not_judged}")
    if paths == 0:
        print("ПЛОХО · судить было нечего")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
