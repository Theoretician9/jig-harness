#!/usr/bin/env python3
"""Проверка приёма комментариев панели: заявка → пинок агенту, журнал доставки.

Спека: docs/specs/hr.kanban.md §4, §7.4. Протокол с диспетчером:
  `comments-intake.py`               — по строке `<ts>\\t<пинок>` на каждую
                                       заявку comments/new/<ts>.json, которой
                                       нет в $LOG_DIR/panel-comments-delivered.jsonl;
  `comments-intake.py --доставлено T` — строка доставки T в тот журнал.
Диспетчер на каждую строку зовёт `agent_kick "<пинок>"`, затем отмечает.

Главное правило: текст комментария НЕ попадает в $LOG_DIR/inbox — оттуда
ворота выката (`deploy_guard._из_очереди`) и одобрение спеки
(`spec_approval.слова_владельца`) берут слова владельца.

Всё на своих временных каталогах (LOG_DIR, PANEL_STATE_DIR, конфиги): боевой
журнал и живая панель не трогаются.

Прогон:
    python3 scripts/test_comments_intake.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTAKE = HERE / "comments-intake.py"

paths_total = 0
failures = 0


def path_ok(text):
    global paths_total
    paths_total += 1
    print("ок  · " + text)


def path_bad(text):
    global paths_total, failures
    paths_total += 1
    failures += 1
    print("ПЛОХО · " + text)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def files_under(directory: Path) -> list:
    if not directory.exists():
        return []
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*"))


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    logs = root / "логи"
    panel_state = root / "панель"
    inbox = logs / "inbox"
    (inbox / "обработано").mkdir(parents=True)
    panel_state.mkdir()
    (root / "дом").mkdir()
    (root / "install.conf").write_text(
        f'LOG_DIR="{logs}"\nPANEL_STATE_DIR="{panel_state}"\n', encoding="utf-8")
    (root / "harness.conf").write_text("CONFIRM_MAX_AGE_MIN=60\n", encoding="utf-8")
    env = dict(os.environ,
               HARNESS_HOME=str(root / "дом"),
               HARNESS_INSTALL_CONF=str(root / "install.conf"),
               HARNESS_CONF=str(root / "harness.conf"),
               LOG_DIR=str(logs),
               HARNESS_LOG_DIR=str(logs),
               PANEL_STATE_DIR=str(panel_state))
    os.environ.update(env)
    delivered = logs / "panel-comments-delivered.jsonl"
    comments = panel_state / "comments"
    claims = comments / "new"

    def intake(*args):
        """(код, строки вывода). Вызов тот же, что у диспетчера: отдельный
        процесс со своим окружением — проба не подменяет функции внутри."""
        try:
            done = subprocess.run([sys.executable, str(INTAKE), *args], env=env,
                                  capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return -1, []
        return done.returncode, [s for s in done.stdout.splitlines() if s.strip()]

    def kicks(lines) -> dict:
        """ts → пинок из строк `<ts>\\t<пинок>`."""
        parsed = {}
        for line in lines:
            ts, sep, kick = line.partition("\t")
            if sep:
                parsed[ts] = kick
        return parsed

    def put_claim(ts: str, task_id: str, text: str) -> None:
        """Заявка так, как её кладёт панель: строка в <id>.jsonl и new/<ts>.json."""
        claims.mkdir(parents=True, exist_ok=True)
        os.chmod(comments, 0o2770)
        os.chmod(claims, 0o2770)
        record = {"ts": ts, "id": task_id, "текст": text}
        with open(comments / f"{task_id}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        (claims / f"{ts}.json").write_text(json.dumps(record, ensure_ascii=False),
                                          encoding="utf-8")

    def delivered_count(ts: str) -> int:
        try:
            rows = [json.loads(s) for s in delivered.read_text(encoding="utf-8").splitlines()
                    if s.strip()]
        except (OSError, ValueError):
            return 0
        return sum(1 for r in rows if str(r.get("ts")) == ts)

    # Модуль на месте и грузится: диспетчер зовёт его первой строкой цикла,
    # и отсутствующий файл — это `|| say` на каждом круге, а не работа.
    try:
        load("comments_intake", INTAKE)
        path_ok("comments-intake.py грузится")
    except Exception as error:
        path_bad(f"comments-intake.py не грузится: {error}")

    # Каталога комментариев ещё нет (панель ни одного не приняла) — пусто и
    # код 0. Диспетчер зовёт приём на КАЖДОМ круге; падение здесь — шум в канал
    # раз в минуту.
    code, lines = intake()
    if code == 0 and not lines:
        path_ok("комментариев нет — вывода нет, код 0")
    else:
        path_bad(f"без комментариев: код {code}, вывод {lines[:2]}")

    t1 = f"{time.time() - 30:.9f}"
    t2 = f"{time.time() - 20:.9f}"
    put_claim(t1, "hr.proba", "первый комментарий к пробе")
    put_claim(t2, "hr.proba", "второй комментарий\nв две строки")

    # Каждая недоставленная заявка — одна строка `<ts>\t<пинок>`. Перевод
    # строки в тексте не вправе разорвать протокол: диспетчер читает построчно,
    # и половина комментария стала бы «заявкой» с мусорным ts.
    code, lines = intake()
    got = kicks(lines)
    if code == 0 and set(got) == {t1, t2} and len(lines) == 2:
        path_ok("две заявки — две строки `<ts>\\t<пинок>`, перевод строки в тексте не рвёт протокол")
    else:
        path_bad(f"вывод приёма: код {code}, строки {lines[:3]}")

    # Пинок говорит агенту, откуда и к какой задаче слово владельца, и несёт
    # сам текст — иначе агент получает «что-то пришло» и идёт искать.
    kick = got.get(t1, "")
    if "Комментарий владельца из панели" in kick and "hr.proba" in kick \
            and "первый комментарий к пробе" in kick:
        path_ok("пинок называет источник, задачу и текст")
    else:
        path_bad(f"пинок не тот: {kick[:120]!r}")

    # Отметка доставки — строка в журнале агента; повтор не дублирует: диспетчер
    # может отметить дважды, если круг прервался после отметки.
    code1, _ = intake("--доставлено", t1)
    code2, _ = intake("--доставлено", t1)
    if code1 == 0 and code2 == 0 and delivered_count(t1) == 1:
        path_ok("--доставлено пишет одну строку, повтор без дубля")
    else:
        path_bad(f"отметка доставки: коды {code1}/{code2}, строк {delivered_count(t1)}")

    # Доставленное больше не печатается — иначе агент получал бы один
    # комментарий на каждом круге диспетчера.
    code, lines = intake()
    got = kicks(lines)
    if code == 0 and t1 not in got and t2 in got:
        path_ok("доставленное не повторяется, недоставленное печатается")
    else:
        path_bad(f"после отметки: код {code}, ts {sorted(got)}")

    # Падение между печатью и отметкой (диспетчер убит после agent_kick, до
    # --доставлено): пинок обязан повториться на следующем круге, а не
    # потеряться. Дубль пинка дешевле потерянного слова владельца.
    code, lines = intake()
    if code == 0 and t2 in kicks(lines) and delivered_count(t2) == 0:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: неотмеченный пинок повторяется — не потерян")
    else:
        path_bad(f"неотмеченная заявка пропала: код {code}, вывод {lines[:2]}")

    # Чужих файлов агент не удаляет: заявки — файлы службы панели, доставка
    # живёт в своём журнале агента.
    if (claims / f"{t1}.json").exists() and (comments / "hr.proba.jsonl").exists():
        path_ok("заявка и журнал панели после доставки на месте")
    else:
        path_bad(f"приём удалил файлы панели: {files_under(comments)}")

    if delivered.exists() and not (panel_state / "panel-comments-delivered.jsonl").exists():
        path_ok("журнал доставки — в LOG_DIR, а не в каталоге панели")
    else:
        path_bad(f"журнала доставки нет в {delivered}")

    # Битая заявка (запись оборвалась) не роняет приём и не прячет соседние:
    # под set -e диспетчера каждое падение — `|| say` и ни одного пинка.
    (claims / f"{time.time() - 10:.9f}.json").write_text('{"ts": "обрыв', encoding="utf-8")
    code, lines = intake()
    if code == 0 and t2 in kicks(lines):
        path_ok("БОЛЬНОЙ СЛУЧАЙ: битая заявка не роняет приём и не прячет соседнюю")
    else:
        path_bad(f"битая заявка: код {code}, вывод {lines[:2]}")

    # ── главное: комментарий «да, выкатывай» не становится согласием ────────
    # Ворота выката и одобрение спеки читают слова владельца из inbox/. Если
    # приём положит туда текст комментария, пропуск панели (веб) станет
    # согласием на выкат мимо канала (контракт, Б1).
    inbox_before = files_under(inbox)
    t_deploy = f"{time.time() - 5:.9f}"
    put_claim(t_deploy, "hr.proba", "да, выкатывай")
    code, lines = intake()
    printed = t_deploy in kicks(lines)
    code_mark, _ = intake("--доставлено", t_deploy)
    inbox_after = files_under(inbox)
    if printed and code_mark == 0 and inbox_after == inbox_before:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: «да, выкатывай» доставлен пинком, во входящих ни одного файла")
    else:
        path_bad(f"«да, выкатывай»: напечатан {printed}, отметка {code_mark}, "
                 f"во входящих появилось {sorted(set(inbox_after) - set(inbox_before))}")

    guard = load("deploy_guard_proba", HERE / "deploy_guard.py")
    approval = load("spec_approval_proba", HERE / "spec_approval.py")

    # Проба обязана уметь видеть согласие — иначе «не нашли» ничего не стоит.
    # Контроль: тот же текст, положенный во входящие ДРУГОГО каталога, ворота
    # находят.
    control = root / "контроль"
    (control / "inbox").mkdir(parents=True)
    (control / "inbox" / "1791999999.txt").write_text("да, выкатывай", encoding="utf-8")
    real_log_dir = guard.LOG_DIR
    guard.LOG_DIR = control
    os.environ["HARNESS_LOG_DIR"] = str(control)
    try:
        sees_guard = any("выкатывай" in r["text"] for r in guard._из_очереди(time.time(), []))
        sees_approval = any("выкатывай" in r["текст"] for r in approval.слова_владельца(0))
    finally:
        guard.LOG_DIR = real_log_dir
        os.environ["HARNESS_LOG_DIR"] = str(logs)
    if sees_guard and sees_approval:
        path_ok("контроль: «да, выкатывай» во входящих ворота и одобрение видят")
    else:
        path_bad(f"контроль слеп: ворота {sees_guard}, одобрение {sees_approval}")

    found_guard = [r["text"] for r in guard._из_очереди(time.time(), [])
                   if "выкатывай" in r["text"]]
    if printed and not found_guard and guard.LOG_DIR == logs:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: deploy_guard._из_очереди согласия из комментария не находит")
    else:
        path_bad(f"ворота выката: комментарий доставлен {printed}, нашли {found_guard}, "
                 f"каталог {guard.LOG_DIR}")

    found_approval = [r["текст"] for r in approval.слова_владельца(0)
                      if "выкатывай" in r["текст"]]
    if printed and not found_approval:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: spec_approval.слова_владельца комментарий не видит")
    else:
        path_bad(f"одобрение спеки: комментарий доставлен {printed}, нашли {found_approval}")

    # ── ревью кода hr.kanban: S-03, S-07, G-09 ──────────────────────────────
    # S-03: заявка — канал в панель агента. Управляющие символы ушли бы
    # нажатиями в TUI (ESC прерывает ход, Ctrl-C закрывает claude).
    intake("--доставлено", t2)
    t_ctrl = f"{time.time() - 4:.9f}"
    put_claim(t_ctrl, "hr.proba", "до\x1b[200~\x03\x15\x7f‮после")
    code, lines = intake()
    kick = kicks(lines).get(t_ctrl, "")
    if code == 0 and kick and not any(ord(c) < 32 or 127 <= ord(c) < 160
                                      or c == "‮" for c in kick) \
            and "до" in kick and "после" in kick:
        path_ok("БОЛЬНОЙ СЛУЧАЙ S-03: управляющие символы из заявки в пинок не идут")
    else:
        path_bad(f"управляющие символы в пинке: код {code}, {kick!r}")
    intake("--доставлено", t_ctrl)

    # S-03: длина и id заявки судятся приёмом, а не только формой панели.
    # Негодная заявка не печатается и отмечается исходом «битая» один раз (G-09).
    t_long = f"{time.time() - 3:.9f}"
    t_badid = f"{time.time() - 2:.9f}"
    put_claim(t_long, "hr.proba", "я" * 5000)
    claims.mkdir(parents=True, exist_ok=True)
    (claims / f"{t_badid}.json").write_text(
        json.dumps({"ts": t_badid, "id": "../x", "текст": "проба"}, ensure_ascii=False),
        encoding="utf-8")
    code, lines = intake()
    got = kicks(lines)

    def outcome(ts: str) -> list:
        try:
            rows = [json.loads(s) for s in delivered.read_text(encoding="utf-8").splitlines()
                    if s.strip()]
        except (OSError, ValueError):
            return []
        return [r.get("исход") for r in rows if str(r.get("ts")) == ts]

    if code == 0 and t_long not in got and t_badid not in got \
            and outcome(t_long) == ["битая"] and outcome(t_badid) == ["битая"]:
        path_ok("БОЛЬНОЙ СЛУЧАЙ S-03: заявка длиннее потолка и с негодным id — "
                "не в пинок, исход «битая»")
    else:
        path_bad(f"негодные заявки: код {code}, напечатаны {sorted(got)}, "
                 f"исходы {outcome(t_long)}/{outcome(t_badid)}")

    # G-09: битая заявка (обрыв JSON выше) отмечена один раз и больше не шумит.
    done = subprocess.run([sys.executable, str(INTAKE)], env=env, capture_output=True,
                          text=True, timeout=30)
    broken_marks = sum(1 for r in (json.loads(s) for s in delivered.read_text(
        encoding="utf-8").splitlines() if s.strip()) if r.get("исход") == "битая")
    if done.returncode == 0 and not done.stderr.strip() and broken_marks == 3:
        path_ok("БОЛЬНОЙ СЛУЧАЙ G-09: битая заявка отмечена один раз, повторный круг молчит")
    else:
        path_bad(f"битые заявки: stderr {done.stderr[:120]!r}, отметок «битая» {broken_marks}")

    # S-07: нечитаемый каталог заявок — это отказ, а не «комментариев нет».
    os.chmod(claims, 0o000)
    try:
        code, lines = intake()
    finally:
        os.chmod(claims, 0o2770)
    if code not in (0, -1) and lines and "не" in " ".join(lines):
        path_ok(f"БОЛЬНОЙ СЛУЧАЙ S-07: каталог заявок не читается → код {code} и причина")
    else:
        path_bad(f"нечитаемый каталог заявок: код {code}, вывод {lines[:2]}")

print(f"ПРИЁМ-КОММЕНТАРИЕВ: путей {paths_total}, неудач {failures}")
sys.exit(1 if failures else 0)
