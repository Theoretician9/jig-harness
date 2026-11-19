#!/usr/bin/env python3
"""Проба work-queue.py: карточка, которая ждёт не смену, а внешнее, — не работа.

Сторож скорости берёт «что взять» из `--готовые`; смена 15.11.2026 получила
~15 толчков подряд на одни и те же карточки, взять которые нельзя: test с
провалом приёмки, когда проба уже починена коммитом (нужна приёмка, не
помощник); выпуск, ждущий кнопку владельца; задача в очереди мутаций среза.

Стенд свой: git-репозиторий (PROJECT_DIR), каталог журналов (LOG_DIR) с
выводом приёмки, журналами кнопок согласия и очередью мутаций, свой /proc
(WORK_QUEUE_PROC_DIR) — и карта. Судится вывод `--готовые`, `--пачки` и строка
«ждут:» настоящего прибора (дверь WORK_QUEUE_SCRIPT — путь прибора).

    python3 scripts/test_work_queue_external_wait.py
Последняя строка: `work-queue внешнее: случаев N, падений F`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("WORK_QUEUE_SCRIPT") or HERE / "work-queue.py")
LINE = "стенд"
FAILED = time.time() - 3600          # приёмка провалилась час назад

CARDS = {
    # провал, после него коммит с трейлером карточки — нужна приёмка
    "hr.retest-trailer": {"status": "test", "провал_проверки": "2026-11-15: команда вернула 1",
                          "проверка": {"команда": "python3 scripts/alert-task.py --is-green x"}},
    # провал, после него коммит в файл пробы из команды — нужна приёмка
    "hr.retest-path": {"status": "test", "провал_проверки": "2026-11-15: команда вернула 1",
                       "проверка": {"команда": "python3 scripts/test_probe_x.py"}},
    # провал, после него ничего не менялось — настоящая починка, работа
    "hr.realfail": {"status": "test", "провал_проверки": "2026-11-15: команда вернула 1",
                    "проверка": {"команда": "python3 scripts/test_untouched.py"}},
    "hr.button": {"status": "plan"},          # кнопка послана, не нажата, срок не вышел
    "hr.button-old": {"status": "plan"},      # кнопка протухла — снова работа
    "hr.publishing": {"status": "plan"},      # «да» нажато, publish.sh идёт
    "hr.mutations": {"status": "plan"},       # в заявке очереди мутаций
    "hr.plain": {"status": "plan"},
}


def card_yaml(ид: str, поля: dict) -> str:
    строки = [f"      - id: {ид}", f"        title: {ид}", f"        status: {поля['status']}"]
    if "провал_проверки" in поля:
        строки.append(f"        провал_проверки: \"{поля['провал_проверки']}\"")
    if "проверка" in поля:
        строки += ["        проверка:", f"          команда: \"{поля['проверка']['команда']}\"",
                   "          ждём: \"ок\""]
    return "\n".join(строки)


def git(root: Path, *args: str, when: float | None = None) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    subprocess.run(["git", "-C", str(root), *args], check=True, env=env,
                   capture_output=True, text=True)


def envelope(event: str, task: str, data: dict, ts: float) -> str:
    stamp = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")
    return json.dumps({"v": 1, "ts": stamp, "src": "стенд", "event": event, "outcome": "ok",
                       "task": task, "data": data}, ensure_ascii=False)


def build(t: Path) -> dict:
    root, log, proc = t / "proj", t / "log", t / "proc"
    for d in (root / "scripts", log / "priyomka", log / "очередь-мутаций", proc / "4242"):
        d.mkdir(parents=True)
    git(root, "init", "-q")
    for name in ("test_probe_x.py", "test_untouched.py", "alert-task.py"):
        (root / "scripts" / name).write_text("# v1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "начало", when=FAILED - 600)
    (root / "scripts" / "test_probe_x.py").write_text("# v2\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "починка пробы", when=FAILED + 600)
    (root / "заметка.md").write_text("x\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "датчик\n\nDev-Map: hr.retest-trailer\nCo-Authored-By: t",
        when=FAILED + 900)
    for ид in ("hr.retest-trailer", "hr.retest-path", "hr.realfail"):
        вывод = log / "priyomka" / f"{ид}.вывод"
        вывод.write_text("упало\n", encoding="utf-8")
        os.utime(вывод, (FAILED, FAILED))
    now = time.time()
    requests = [envelope("consent.request", "hr.button", {"nonce": "n-button", "вид": "публикация",
                                                          "цель": "x"}, now - 600),
                envelope("consent.request", "hr.button-old", {"nonce": "n-old", "вид": "публикация",
                                                              "цель": "x"}, now - 5 * 86400),
                envelope("consent.request", "hr.publishing,", {"nonce": "n-pub", "вид": "публикация",
                                                               "цель": "x"}, now - 900)]
    (log / "consent-requests.jsonl").write_text("\n".join(requests) + "\n", encoding="utf-8")
    buttons = [envelope("consent.sent", "hr.button", {"nonce": "n-button"}, now - 590),
               envelope("consent.yes", "hr.publishing", {"nonce": "n-pub"}, now - 800)]
    (log / "consent-buttons.jsonl").write_text("\n".join(buttons) + "\n", encoding="utf-8")
    (log / "очередь-мутаций" / "очередь.json").write_text(json.dumps(
        {"заявки": [{"знак": "1", "ids": ["hr.mutations"], "команда": ["x"], "с": now, "заход": None}],
         "итоги": {}}, ensure_ascii=False), encoding="utf-8")
    (proc / "4242" / "cmdline").write_bytes(b"/bin/bash\0/var/www/x/scripts/publish.sh\0")
    карта = t / "dev-map.yaml"
    карта.write_text(f"lines:\n  - id: {LINE}\n    tasks:\n"
                     + "\n".join(card_yaml(i, п) for i, п in CARDS.items()) + "\n", encoding="utf-8")
    env = dict(os.environ, PROJECT_DIR=str(root), LOG_DIR=str(log), HARNESS_LOG_DIR=str(log),
               HARNESS_ACTIVE_LINE=LINE, WORK_QUEUE_PROC_DIR=str(proc), PYTHONDONTWRITEBYTECODE="1")
    return {"map": карта, "env": env}


def run(stand: dict, *flags: str) -> str:
    done = subprocess.run([sys.executable, str(QUEUE), str(stand["map"]), *flags],
                          capture_output=True, text=True, env=stand["env"], timeout=60)
    return done.stdout + done.stderr


def main() -> int:
    cases = fails = 0

    def case(name: str, ok: bool, got: str = "") -> None:
        nonlocal cases, fails
        cases += 1
        if not ok:
            fails += 1
        print(f"  {'ок   ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f" — вышло: {got[:300]}"))

    with tempfile.TemporaryDirectory(prefix="wq-external-") as tmp:
        stand = build(Path(tmp))
        ready_out = run(stand, "--готовые")
        ready = ready_out.splitlines()[1:]
        if not ready_out.startswith("готовых:"):
            case("прибор отвечает «готовых: N»", False, ready_out)
            print(f"work-queue внешнее: случаев {cases}, падений {fails}")
            return 1
        waits = next((l for l in run(stand).splitlines() if l.startswith("ждут:")), "")
        bundles = run(stand, "--пачки")
        reasons = dict(p.split("=", 1) for p in waits[len("ждут: "):].split("; ") if "=" in p)

        in_bundles = {i.strip() for l in bundles.splitlines()[1:] for i in l.split(":", 1)[-1].split(",")}

        def absent(ид: str) -> bool:
            return ид not in ready and ид not in in_bundles

        case("провал и коммит с трейлером после него — не работа", absent("hr.retest-trailer"), ready_out)
        case("провал и коммит с трейлером — в «ждут» повтор приёмки",
             "приёмк" in reasons.get("hr.retest-trailer", ""), waits)
        case("провал и коммит в файл пробы после него — не работа", absent("hr.retest-path"), ready_out)
        case("провал и коммит в файл пробы — в «ждут» повтор приёмки",
             "приёмк" in reasons.get("hr.retest-path", ""), waits)
        case("провал без перемен после него — работа (починка за сменой)", "hr.realfail" in ready, ready_out)
        case("кнопка послана и не нажата — не работа", absent("hr.button"), ready_out)
        case("кнопка послана — в «ждут» кнопка владельца", "кнопк" in reasons.get("hr.button", ""), waits)
        case("кнопка протухла — снова работа", "hr.button-old" in ready, ready_out)
        case("«да» нажато и publish.sh идёт — не работа", absent("hr.publishing"), ready_out)
        case("«да» и publish.sh — в «ждут» выпуск", "выпуск" in reasons.get("hr.publishing", ""), waits)
        case("заявка в очереди мутаций — не работа", absent("hr.mutations"), ready_out)
        case("очередь мутаций — в «ждут» мутации", "мутаци" in reasons.get("hr.mutations", ""), waits)
        case("простая plan — работа", "hr.plain" in ready, ready_out)
        # Без publish.sh в /proc «да» уже не держит карточку: процесса нет — выпуск кончился.
        (Path(tmp) / "proc" / "4242" / "cmdline").write_bytes(b"/bin/bash\0/x/scripts/other.sh\0")
        ready2 = run(stand, "--готовые").splitlines()[1:]
        case("publish.sh кончился — карточка снова работа", "hr.publishing" in ready2, " ".join(ready2))
    print(f"work-queue внешнее: случаев {cases}, падений {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
