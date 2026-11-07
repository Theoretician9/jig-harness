#!/usr/bin/env python3
"""Пробы закона полноты библиотеки процессов (hr.process-library-completeness-gate).

Владелец 06.11: «как будет обновляться информация в библиотеке и диспетчере о
изменяемых, убираемых, добавляющихся процессах и механизмах? Есть какой-то
железобетонный механизм, который это будет контролировать независимо, системно,
точно?»

Часть 1 — судья scripts/check-process-library.py. Двусторонняя сверка источников
запуска с библиотекой (harness/config/process-library.json + .yaml) и поля
«механизмы» с mechanisms.yaml. Проба строит свой проект-стенд во временном
каталоге: демоны, хуки, библиотека, подставные crontab и список юнитов. Живая
машина не читается (кроме случая --живой).

Часть 2 — сторож доли «не разобрано» в process-dispatcher.py --замер: доля
выше PROCESS_UNPARSED_ALERT_PCT (harness.conf, умолчание 15) за окно 1 ч заводит
тревогу через <проект>/scripts/alert-task.py с pid, comm, cgroup процесса.
Стенд: поддельные /proc и cgroup (write_snapshot пробы диспетчера), свой журнал
process-load.jsonl с выборками прошлого часа, писатель тревог — заглушка,
которая только записывает свои доводы.

Контракт судьи:
    check-process-library.py [--root R] [--crontab FILE] [--units FILE] [--project-dir P]
    R — корень проекта (умолчание — проект судьи); FILE — подставной crontab /
    список юнитов по одному имени в строке (без ключа — `crontab -l` и юниты
    systemd harness-*); P — префикс своих путей в строках крона (умолчание R).
    Коды: 0 чисто · 1 нарушения · 2 кривой вызов (нет библиотеки под R) ·
    3 источник недоступен (crontab или systemctl нет / отдал ошибку): строка
    `check-process-library: источник недоступен: crontab|systemctl` — пустой
    живой список не выдаётся за «чисто».
    Строка нарушения: `НАРУШЕНИЕ <вид>: <источник> :: <ключ>`,
    вид ∈ {без записи, мёртвая запись, нет механизма}. Нарушение по живой машине
    (источник crontab или systemd) дописывает «живая машина» и «коммит ни при чём».
    Строка крона: на каждый свой путь (после `cd P &&` — и относительный, в кавычках,
    внутри `bash -c '…'`, после `;`) — своя запись; обёртка run-background.sh <id> --
    требует живую запись id, а её команда — путь из «файлов» этой записи.
    Обратная сторона: запись «хук» — её файл в .claude/settings.json; «служба» —
    юнит из «юниты» или harness-<id>/<id>.service|.timer; «крон» — живая
    (не закомментированная) строка крона с id или файлом записи; иначе
    `НАРУШЕНИЕ мёртвая запись: <id> :: .claude/settings.json|<юнит>|crontab`.
    Итог (последняя строка): `check-process-library: чисто, записей N` или
    `check-process-library: нарушений N`.

Двери пробы:
    HARNESS_CHECK_PROCESS_LIBRARY  путь к судье (умолчание — рядом с пробой)
    HARNESS_PROCESS_DISPATCHER     путь к process-dispatcher.py
Прогон (только фоном):
    python3 scripts/test_process_library_gate.py [--живой]
Последняя строка: `ЗАКОН БИБЛИОТЕКИ ПРОЦЕССОВ: путей N, неудач F`.
"""
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
JUDGE = Path(os.environ.get("HARNESS_CHECK_PROCESS_LIBRARY") or HERE / "check-process-library.py")
DISPATCHER = Path(os.environ.get("HARNESS_PROCESS_DISPATCHER") or HERE / "process-dispatcher.py")
LIVE = "--живой" in sys.argv
SECRET = "ZZSECRET" + uuid.uuid4().hex[:12]  # не-секрет: подставной маркер пробы
TMP = Path(tempfile.mkdtemp(prefix="proba-process-library-gate-"))
ALERT_KEY = "process-dispatcher:не-разобрано"
TIMEOUT = 120

paths = failures = 0


def report(ok: bool, text: str, bad=()) -> None:
    global paths, failures
    paths += 1
    if ok:
        print("ок    · " + text)
        return
    failures += 1
    bad = [str(b).replace(SECRET, "<SECRET>") for b in bad]
    print("КРАСН · " + text + (": " + "; ".join(bad[:8]) if bad else ""))


def config_dir() -> Path:
    # Раскладки проекта и пакета разные: конфиги там, где config/process-library.json.
    for folder in HERE.parents:
        if (folder / "config" / "process-library.json").is_file():
            return folder / "config"
    return HERE.parent / "harness" / "config"


def run(cmd: list, env=None) -> tuple:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "таймаут"
    return done.returncode, (done.stdout + done.stderr).strip()


# ───────────────────────── часть 1: стенд судьи ─────────────────────────

def record(ident, launch, files, mechanisms=None, status="работает", cls="фоновый", **extra) -> dict:
    rec = {"id": ident, "название": ident, "класс": cls, "запуск": launch, "статус": status, "файлы": files}
    if mechanisms is not None:
        rec["механизмы"] = mechanisms
    rec.update(extra)
    return rec


class Stand:
    """Чистый проект: каждый источник запуска имеет запись, каждая запись — живой источник."""

    def __init__(self, name: str):
        self.root = TMP / f"j-{name}"
        self.config = self.root / "harness" / "config"
        for d in (self.config, self.root / "harness" / "demons", self.root / "scripts" / "hooks",
                  self.root / ".claude"):
            d.mkdir(parents=True)
        for rel in ("harness/demons/alpha.sh", "harness/demons/alpha_check.py", "harness/demons/beta.sh",
                    "scripts/panel.py", "scripts/start-agent.sh", "scripts/hooks/guard.py"):
            self.touch(rel)
        self.library = {"версия": 1, "процессы": [
            record("alpha", "крон", ["harness/demons/alpha.sh", "harness/demons/alpha_check.py"],
                   ["harness/demons/alpha.sh"]),
            record("beta", "служба-проверок", ["harness/demons/beta.sh"], ["harness/demons/beta.sh"]),
            record("harness-panel", "служба", ["scripts/panel.py"]),
            # Имя юнита не совпадает с id: связь задаёт поле «юниты».
            record("claude-session", "служба", ["scripts/start-agent.sh"], юниты=["harness-agent.service"]),
            record("hook-guard", "хук", ["scripts/hooks/guard.py"], cls="по внутреннему действию"),
            # Снятая запись: файла нет, расписания нет — судья её не трогает.
            record("gone", "крон", ["harness/demons/gone.sh", "/var/log/harness/gone.log"], status="снят 05.11"),
        ]}
        self.schedule = {"alpha": ("крон", "*/5 * * * *", "harness/demons/alpha.sh"),
                         "beta": ("служба-проверок", "0 2 * * *", "harness/demons/beta.sh")}
        self.mechanisms = ["harness/demons/alpha.sh", "harness/demons/beta.sh"]
        self.hooks = ["scripts/hooks/guard.py"]
        self.cron = [f"*/5 * * * * {self.root}/scripts/run-background.sh alpha -- "
                     f"{self.root}/harness/demons/alpha.sh >>/tmp/alpha.log 2>&1",
                     "0 3 * * * /usr/bin/certbot renew --quiet",
                     f"#ВЫКЛ-0511 */5 * * * * {self.root}/harness/demons/gone.sh",
                     "# комментарий владельца"]
        self.units = ["harness-panel.service", "harness-agent.service", "harness-beta.service",
                      "harness-beta.timer", "harness-checks.slice"]
        self.crontab_file = self.root.parent / f"{name}.crontab"
        self.units_file = self.root.parent / f"{name}.units"

    def touch(self, rel: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")

    def write(self) -> None:
        (self.config / "process-library.json").write_text(
            json.dumps(self.library, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        lines = ["версия: 1", "процессы:"]
        for ident, (launch, cron, command) in self.schedule.items():
            lines += [f"  {ident}:", f"    запуск: {launch}", f'    расписание: "{cron}"',
                      f'    команда: "{command}"', f"    журнал: {ident}.log"]
        (self.config / "process-library.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.config / "mechanisms.yaml").write_text(
            "механизмы:\n" + "".join(f"- файл: {m}\n  вид: демон\n  имя: {m}\n" for m in self.mechanisms),
            encoding="utf-8")
        hooks = [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 $CLAUDE_PROJECT_DIR/{h}"}]}
                 for h in self.hooks]
        (self.root / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": hooks}}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.crontab_file.write_text("\n".join(self.cron) + "\n", encoding="utf-8")
        self.units_file.write_text("\n".join(self.units) + "\n", encoding="utf-8")

    def judge(self) -> tuple:
        self.write()
        rc, out = run([sys.executable, str(JUDGE), "--root", str(self.root),
                       "--crontab", str(self.crontab_file), "--units", str(self.units_file)])
        return rc, out, [line for line in out.splitlines() if line.startswith("НАРУШЕНИЕ ")]

    def fake_bin(self, crontab=True, systemctl=True, crontab_fails=False) -> Path:
        """Каталог для PATH с подставными crontab и systemctl: судья зовёт их сам, без ключей."""
        bindir = self.root.parent / f"{self.root.name}-bin"
        bindir.mkdir(exist_ok=True)
        tools = {}
        if crontab:
            tools["crontab"] = ("echo 'crontab: cannot connect to daemon' >&2; exit 2" if crontab_fails
                                else f"/bin/cat {self.crontab_file}")
        if systemctl:
            tools["systemctl"] = f"/bin/cat {self.units_file}"
        for name, body in tools.items():
            (bindir / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            (bindir / name).chmod(0o755)
        return bindir


def judged_red(title: str, stand: Stand, kind: str, key: str, source: str = "") -> None:
    """Ровно одно нарушение нужного вида, с ключом и источником, код 1."""
    rc, out, found = stand.judge()
    hits = [line for line in found if f"НАРУШЕНИЕ {kind}:" in line and key in line and source in line]
    tail = out.splitlines()[-1][:200] if out else "пусто"
    report(rc == 1 and len(found) == 1 and bool(hits),
           f"{title}: код 1, одно «{kind}» с ключом {key}" + (f" и источником {source}" if source else ""),
           [f"rc={rc}", f"нарушений {len(found)}: {found[:3]}", f"хвост «{tail}»"])


def case_clean() -> None:
    print("── 1. чистый стенд → зелёный, записи посчитаны разбором ──")
    stand = Stand("clean")
    rc, out, found = stand.judge()
    wanted = len(stand.library["процессы"])
    tail = out.splitlines()[-1] if out else ""
    report(rc == 0 and not found and tail == f"check-process-library: чисто, записей {wanted}",
           f"чистый стенд: код 0, итог «чисто, записей {wanted}»",
           [f"rc={rc}", f"нарушения {found[:3]}", f"хвост «{tail[:200]}»"])


def case_new_demon() -> None:
    print("── 2. новый демон без записи ──")
    stand = Stand("new-demon")
    stand.touch("harness/demons/gamma.sh")
    judged_red("новый демон", stand, "без записи", "harness/demons/gamma.sh", "harness/demons")


def case_record_without_source() -> None:
    print("── 3. запись крона без расписания и строки крона ──")
    stand = Stand("no-source")
    stand.library["процессы"].append(record("delta", "крон", ["harness/demons/alpha.sh"]))
    judged_red("запись без источника", stand, "мёртвая запись", "delta", "process-library.json")


def case_cron_wrapped() -> None:
    print("── 4а. строка крона через обёртку с id вне библиотеки ──")
    stand = Stand("cron-wrapped")
    stand.cron.append(f"0 4 * * * {stand.root}/scripts/run-background.sh omega -- "
                      f"{stand.root}/harness/demons/alpha.sh >>/tmp/omega.log 2>&1")
    judged_red("строка крона (обёртка)", stand, "без записи", "omega", "crontab")


def case_cron_bare() -> None:
    print("── 4б. своя строка крона без обёртки, путь вне «файлов» ──")
    stand = Stand("cron-bare")
    stand.touch("scripts/stray.sh")
    stand.cron.append(f"0 5 * * * {stand.root}/scripts/stray.sh >>/tmp/stray.log 2>&1")
    judged_red("строка крона (без обёртки)", stand, "без записи", "scripts/stray.sh", "crontab")


def case_hook() -> None:
    print("── 5. хук без записи ──")
    stand = Stand("hook")
    stand.touch("scripts/hooks/newhook.py")
    stand.hooks.append("scripts/hooks/newhook.py")
    judged_red("хук", stand, "без записи", "scripts/hooks/newhook.py", ".claude/settings.json")


def case_ghost_mechanism() -> None:
    print("── 6. запись с несуществующим механизмом ──")
    stand = Stand("ghost")
    stand.library["процессы"][0]["механизмы"].append("harness/demons/ghost.sh")
    judged_red("механизм", stand, "нет механизма", "harness/demons/ghost.sh", "alpha")


def case_removed_demon() -> None:
    print("── 7. демон удалён, запись осталась ──")
    stand = Stand("removed")
    (stand.root / "harness" / "demons" / "beta.sh").unlink()
    stand.mechanisms.remove("harness/demons/beta.sh")
    stand.library["процессы"][1]["механизмы"] = []
    judged_red("удалённый демон", stand, "мёртвая запись", "harness/demons/beta.sh", "beta")


def case_unit() -> None:
    print("── 8. юнит harness-* без записи ──")
    stand = Stand("unit")
    stand.units.append("harness-zeta.service")
    judged_red("юнит systemd", stand, "без записи", "harness-zeta.service")


def case_schedule() -> None:
    print("── 9. расписание process-library.yaml без записи JSON ──")
    stand = Stand("schedule")
    stand.schedule["epsilon"] = ("крон", "0 6 * * *", "harness/demons/alpha.sh")
    judged_red("расписание", stand, "без записи", "epsilon", "process-library.yaml")


def case_empty_root() -> None:
    print("── 10. корень без библиотеки → кривой вызов, не «чисто» ──")
    empty = TMP / "j-empty"
    empty.mkdir()
    rc, out = run([sys.executable, str(JUDGE), "--root", str(empty), "--crontab", "/dev/null",
                   "--units", "/dev/null"])
    # Без проверки наличия судьи код 2 дал бы сам python3 («can't open file»).
    report(JUDGE.exists() and rc == 2, "пустой корень: код 2 от судьи", [f"rc={rc}", f"хвост «{out.splitlines()[-1][:200] if out else ''}»"])


def case_wiring() -> None:
    print("── 11. судья стоит в pre-commit и в воротах на КАЖДОМ коммите ──")
    pre = (HERE / "pre-commit-gate.sh").read_text(encoding="utf-8")
    gates = (HERE / "gates.sh").read_text(encoding="utf-8")
    # С нулевого отступа: строка внутри `if … CHANGED | grep …; then` судила бы только правку библиотеки.
    pre_hits = re.findall(r'(?m)^гейт\s+"[^"]+"\s+(?:на_снимке\s+)?python3\s+"\$SNAP_DIR/scripts/'
                          r'check-process-library\.py"\s+--root\s+"\$SNAP_DIR"', pre)
    gates_hits = re.findall(r'(?m)^проверка\s+"[^"]+"\s+python3\s+scripts/check-process-library\.py', gates)
    report(len(pre_hits) == 1, "pre-commit-gate.sh: гейт судьи с нулевого отступа, --root \"$SNAP_DIR\"",
           [f"найдено {len(pre_hits)}"])
    report(len(gates_hits) == 1, "gates.sh: проверка судьи с нулевого отступа", [f"найдено {len(gates_hits)}"])


def case_precommit_live_prefix() -> None:
    print("── 17. pre-commit: судья на снимке с --project-dir, голая строка крона с боевым префиксом судится ──")
    pre = (HERE / "pre-commit-gate.sh").read_text(encoding="utf-8")
    lines = re.findall(r'(?m)^гейт\s+"[^"]+"\s+(?:на_снимке\s+)?python3\s+"\$SNAP_DIR/scripts/'
                       r'check-process-library\.py".*$', pre)
    report(len(lines) == 1 and "--project-dir" in lines[0], "pre-commit-gate.sh: вызов судьи несёт --project-dir",
           [f"строки {lines[:2]}"])
    if len(lines) != 1:
        return
    live = Stand("pc-live")
    live.touch("scripts/stray-unknown.sh")
    live.cron.append(f"0 5 * * * {live.root}/scripts/stray-unknown.sh >>/tmp/stray.log 2>&1")
    live.write()
    snap = TMP / "j-pc-snap"
    shutil.copytree(live.root, snap)
    (snap / "scripts").mkdir(exist_ok=True)
    shutil.copy2(JUDGE, snap / "scripts" / "check-process-library.py")
    bindir = live.fake_bin()
    # Строка гейта исполняется как есть: pre-commit идёт из корня проекта, гейт и на_снимке — сквозные.
    script = (f'cd "{live.root}" || exit 99\nSNAP_DIR="{snap}"\nSCRIPT_DIR="{snap}/scripts"\n'
              f'PROJECT_DIR="{live.root}"\nна_снимке() {{ "$@"; }}\nгейт() {{ shift; "$@"; }}\n{lines[0]}\n')
    env = dict(os.environ, PATH=f"{bindir}:/usr/bin:/bin")
    rc, out = run(["bash", "-c", script], env)
    found = [l for l in out.splitlines() if l.startswith("НАРУШЕНИЕ ") and "scripts/stray-unknown.sh" in l]
    report(rc == 1 and len(found) == 1, "строка гейта на снимке: незнакомый скрипт в живом кроне → нарушение",
           [f"rc={rc}", f"хвост «{(out.splitlines() or [''])[-1][:200]}»"])


def case_cron_bypasses() -> None:
    print("── 18. обходы разбора строки крона: каждая форма даёт одно нарушение ──")
    forms = [
        ("cd && ./x", lambda r: f"0 5 * * * cd {r} && ./scripts/stray.sh >>/tmp/s.log 2>&1", "scripts/stray.sh"),
        ("bash -c", lambda r: f"0 5 * * * bash -c '{r}/scripts/stray.sh'", "scripts/stray.sh"),
        ("обёртка в кавычках", lambda r: f'0 5 * * * "{r}/scripts/run-background.sh" omega -- {r}/harness/demons/alpha.sh',
         "omega"),
        ("второй скрипт в строке", lambda r: f"0 5 * * * {r}/scripts/panel.py; {r}/scripts/stray.sh", "scripts/stray.sh"),
        ("известный id, чужая команда",
         lambda r: f"0 5 * * * {r}/scripts/run-background.sh alpha -- {r}/harness/demons/beta.sh", "harness/demons/beta.sh"),
        ("id снятой записи", lambda r: f"0 5 * * * {r}/scripts/run-background.sh gone -- {r}/harness/demons/alpha.sh",
         "gone"),
    ]
    for i, (title, line, key) in enumerate(forms):
        stand = Stand(f"bypass{i}")
        stand.touch("scripts/stray.sh")
        stand.cron.append(line(stand.root))
        judged_red(f"обход «{title}»", stand, "без записи", key, "crontab")


def case_dead_sources() -> None:
    print("── 19. источник убран, запись жива → «мёртвая запись» ──")
    stand = Stand("dead-hook")
    stand.hooks = []
    judged_red("хук убран из settings.json", stand, "мёртвая запись", ".claude/settings.json", "hook-guard")
    stand = Stand("dead-unit")
    stand.units.remove("harness-panel.service")
    judged_red("служба без юнита", stand, "мёртвая запись", "harness-panel.service", "harness-panel")
    stand = Stand("dead-cron-off")
    stand.cron[0] = "#ВЫКЛ-0611 " + stand.cron[0]
    judged_red("строка крона выключена #ВЫКЛ", stand, "мёртвая запись", "crontab", "alpha")
    stand = Stand("dead-cron-gone")
    del stand.cron[0]
    judged_red("строка крона убрана", stand, "мёртвая запись", "crontab", "alpha")


def case_sources_unavailable() -> None:
    print("── 20. crontab / systemctl недоступны → код 3 вслух, не «чисто» ──")
    variants = [("crontab нет", dict(crontab=False), "crontab"),
                ("crontab отдал ошибку", dict(crontab_fails=True), "crontab"),
                ("systemctl нет", dict(systemctl=False), "systemctl")]
    for i, (title, kw, source) in enumerate(variants):
        stand = Stand(f"unavail{i}")
        stand.write()
        bindir = stand.fake_bin(**kw)
        # В PATH только свой каталог: настоящие crontab/systemctl машины не видны, судья зовётся по полному пути.
        env = dict(os.environ, PATH=str(bindir))
        rc, out = run([sys.executable, str(JUDGE), "--root", str(stand.root)], env)
        said = f"источник недоступен: {source}" in out
        report(rc == 3 and said and "чисто" not in out, f"{title}: код 3 и «источник недоступен: {source}»",
               [f"rc={rc}", f"хвост «{(out.splitlines() or [''])[-1][:200]}»"])
    stand = Stand("avail")
    stand.write()
    env = dict(os.environ, PATH=str(stand.fake_bin()))
    rc, out = run([sys.executable, str(JUDGE), "--root", str(stand.root)], env)
    report(rc == 0, "контроль: подставные crontab и systemctl на месте → код 0",
           [f"rc={rc}", f"хвост «{(out.splitlines() or [''])[-1][:200]}»"])


def case_live_machine_wording() -> None:
    print("── 21. нарушение по живой машине так и называется ──")
    for name, mutate, key in (
            ("cron", lambda s: s.cron.append(f"0 4 * * * {s.root}/scripts/run-background.sh omega -- "
                                             f"{s.root}/harness/demons/alpha.sh"), "omega"),
            ("unit", lambda s: s.units.append("harness-zeta.service"), "harness-zeta.service")):
        stand = Stand(f"wording-{name}")
        mutate(stand)
        rc, out, found = stand.judge()
        hits = [l for l in found if key in l]
        report(rc == 1 and len(hits) == 1 and "живая машина" in hits[0] and "коммит ни при чём" in hits[0],
               f"{name}: нарушение говорит «живая машина» и «коммит ни при чём»", [f"rc={rc}", f"строки {found[:2]}"])
    stand = Stand("wording-demon")
    stand.touch("harness/demons/gamma.sh")
    rc, out, found = stand.judge()
    report(len(found) == 1 and "живая машина" not in found[0], "демон из индекса не помечен «живая машина»",
           [f"строки {found[:2]}"])


def case_live() -> None:
    print("── Ж. живой проект: crontab -l и юниты машины ──")
    root = HERE.parent if (HERE.parent / "harness" / "config").is_dir() else HERE.parent.parent
    rc, out = run([sys.executable, str(JUDGE), "--root", str(root)])
    report(rc == 0, f"живой проект {root}: код 0", out.splitlines()[-8:])


# ───────────────────────── часть 2: сторож «не разобрано» ─────────────────────────

def load_dispatcher_probe():
    """write_snapshot и proc — из пробы диспетчера: одна форма поддельного /proc на обе пробы."""
    spec = importlib.util.spec_from_file_location("probe_dispatcher", HERE / "test_process_dispatcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shutil.rmtree(module.TMP, ignore_errors=True)   # её каталог при импорте нам не нужен
    return module


STUB_WRITER = """#!/usr/bin/env python3
import json, sys
with open({calls!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
"""
CORES = 6
MYSTERY_CG = "/system.slice/mystery.service"


class Watch:
    def __init__(self, name: str, pct=None):
        self.dir = TMP / f"w-{name}"
        self.logs, self.state, self.project = self.dir / "logs", self.dir / "state", self.dir / "project"
        for d in (self.logs / ".events" / "runs", self.state / "heartbeat", self.project / "scripts",
                  self.dir / "transcripts"):
            d.mkdir(parents=True)
        self.calls = self.dir / "writer-calls.jsonl"
        writer = self.project / "scripts" / "alert-task.py"
        writer.write_text(STUB_WRITER.format(calls=str(self.calls)), encoding="utf-8")
        writer.chmod(0o755)
        self.library = self.dir / "process-library.json"
        self.library.write_text(json.dumps({"версия": 1, "процессы": []}, ensure_ascii=False), encoding="utf-8")
        conf = f"LOG_DIR={self.logs}\nSTATE_DIR={self.state}\nBACKGROUND_PROCESSES=on\n"
        if pct is not None:
            conf += f'PROCESS_UNPARSED_ALERT_PCT="{pct}"\n'
        (self.dir / "harness.conf").write_text(conf, encoding="utf-8")
        (self.dir / "install.conf").write_text("", encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("PROCESS_UNPARSED_ALERT_PCT", "CLAUDE_CODE_SESSION_ID")}
        self.env.update(HARNESS_LOG_DIR=str(self.logs), LOG_DIR=str(self.logs), STATE_DIR=str(self.state),
                        HEARTBEAT_DIR=str(self.state / "heartbeat"), HARNESS_EVENTS_STATE=str(self.logs / ".events"),
                        PROCESS_LOAD_STATE=str(self.logs / "process-load.state.json"),
                        HARNESS_CONF=str(self.dir / "harness.conf"),
                        HARNESS_INSTALL_CONF=str(self.dir / "install.conf"), HARNESS_TASK="-")
        self.snaps = 0

    def history(self, minutes: int, pct: float) -> None:
        """Выборки process.sample раз в минуту за прошлые `minutes` минут с долей «не разобрано» pct."""
        now = dt.datetime.now().astimezone()
        capacity = 60.0 * CORES
        with (self.logs / "process-load.jsonl").open("a", encoding="utf-8") as f:
            for back in range(minutes, 0, -1):
                row = {"v": 1, "ts": (now - dt.timedelta(minutes=back)).isoformat(timespec="seconds"),
                       "src": "scripts/process-dispatcher.py", "event": "process.sample", "outcome": "ok",
                       "task": "-", "data": {"окно_с": 60.0, "ядер": CORES, "группы": [],
                                             "остаток": {"mystery.service": capacity * pct / 100}}}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def collect(self, probe, uptime: float, used: float) -> tuple:
        self.snaps += 1
        init = probe.proc(1, 0, 1, "systemd", ["/sbin/init"], "/init.scope")
        mystery = probe.proc(4242, 1, 4242, "mysteryd", ["/usr/local/bin/mysteryd", "--token", SECRET],
                             MYSTERY_CG, cpu=(used, 0, 0, 0), start=500.0)
        proc_dir, cg_dir = probe.write_snapshot(self.dir / f"snap{self.snaps}", uptime, [init, mystery],
                                                {MYSTERY_CG: used}, cores=CORES)
        return run([sys.executable, str(DISPATCHER), "--замер", "--proc", str(proc_dir), "--cgroup", str(cg_dir),
                    "--библиотека", str(self.library), "--проект", str(self.project),
                    "--транскрипты", str(self.dir / "transcripts"), "--журналы", str(self.logs)], self.env)

    def samples(self) -> int:
        text = (self.logs / "process-load.jsonl").read_text(encoding="utf-8")
        return sum(1 for line in text.splitlines() if '"process.sample"' in line)

    def alerts(self) -> list:
        if not self.calls.is_file():
            return []
        argvs = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines() if line]
        return [a for a in argvs if "--key" in a and a[a.index("--key") + 1:a.index("--key") + 2] == [ALERT_KEY]]


def watch_case(probe, name: str, minutes: int, pct: float, conf_pct=None) -> tuple:
    w = Watch(name, conf_pct)
    w.history(minutes, pct)
    rc1, out1 = w.collect(probe, 1000.0, 10.0)
    rc2, out2 = w.collect(probe, 1060.0, 40.0)
    # Стенд живой, только если второй проход дописал свою выборку: иначе «тревоги нет» — пустой зелёный.
    alive = rc1 == 0 and rc2 == 0 and w.samples() == minutes + 1
    report(alive, f"стенд {name}: два прохода --замер, дописана одна выборка",
           [f"rc={rc1},{rc2}", f"выборок {w.samples()} при истории {minutes}", (out2.splitlines() or [""])[-1][:160]])
    return w.alerts(), [f"проходы rc={rc1},{rc2}", (out2.splitlines() or [""])[-1][:160]]


def case_watch(probe) -> None:
    print("── 12. доля «не разобрано» 20 % > 15 % дольше часа → тревога с pid, comm, cgroup ──")
    alerts, why = watch_case(probe, "high", 66, 20.0)
    text = " ".join(alerts[0]) if alerts else ""
    report(len(alerts) >= 1 and "--watcher" in alerts[0] and "process-dispatcher" in alerts[0],
           f"alert-task.py позван с --key {ALERT_KEY} --watcher process-dispatcher", why + [f"вызовы {alerts[:2]}"])
    report(all(s in text for s in ("4242", "mysteryd", MYSTERY_CG)),
           "текст тревоги называет pid 4242, comm mysteryd, cgroup " + MYSTERY_CG, [text[:300]])
    report(bool(text) and SECRET not in text, "командная строка процесса в тревогу не попала", [])

    print("── 13. доля 5 % ниже порога → тревоги нет ──")
    alerts, why = watch_case(probe, "low", 66, 5.0)
    report(not alerts, "ниже порога: alert-task.py с ключом не звали", why + [f"вызовы {alerts[:2]}"])

    print("── 14. доля 20 %, но только 30 минут → тревоги нет ──")
    alerts, why = watch_case(probe, "short", 30, 20.0)
    report(not alerts, "окно короче часа: тревоги нет", why + [f"вызовы {alerts[:2]}"])

    print("── 15. порог из harness.conf: 30 % при доле 20 % → тревоги нет ──")
    alerts, why = watch_case(probe, "conf", 66, 20.0, conf_pct=30)
    report(not alerts, "PROCESS_UNPARSED_ALERT_PCT=30 читается из harness.conf", why + [f"вызовы {alerts[:2]}"])

    print("── 22. тревога заведена, доля упала ниже порога → --resolve ──")
    w = Watch("resolve")
    w.history(66, 20.0)
    w.collect(probe, 1000.0, 10.0)
    w.collect(probe, 1060.0, 40.0)
    raised = bool(w.alerts())
    (w.logs / "process-load.jsonl").write_text("", encoding="utf-8")
    w.history(66, 2.0)
    rc, out = w.collect(probe, 1120.0, 40.5)
    argvs = [json.loads(line) for line in w.calls.read_text(encoding="utf-8").splitlines() if line] \
        if w.calls.is_file() else []
    last_alert = max((i for i, a in enumerate(argvs) if "--key" in a), default=-1)
    resolved = [i for i, a in enumerate(argvs) if a[-2:] == ["--resolve", ALERT_KEY] and i > last_alert]
    report(raised and rc == 0 and bool(resolved), "после тревоги доля 2 % → alert-task.py --resolve " + ALERT_KEY,
           [f"тревога была {raised}", f"rc={rc}", f"вызовы {argvs[-3:]}"])

    print("── 16. ключ в образце harness.conf ──")
    sys.path.insert(0, str(HERE / "lib"))
    from config import из_файла  # общий разбор конфига, а не своя регулярка
    found = из_файла(str(config_dir() / "harness.conf")).get("PROCESS_UNPARSED_ALERT_PCT")
    report(found == "15", "образец harness.conf: PROCESS_UNPARSED_ALERT_PCT=15", [f"найдено {found!r}"])


def main() -> int:
    if not JUDGE.exists():
        print(f"кода нет: {JUDGE} — случаи части 1 ниже красные по этой причине")
    try:
        for fn in (case_clean, case_new_demon, case_record_without_source, case_cron_wrapped, case_cron_bare,
                   case_hook, case_ghost_mechanism, case_removed_demon, case_unit, case_schedule,
                   case_empty_root, case_wiring, case_precommit_live_prefix, case_cron_bypasses,
                   case_dead_sources, case_sources_unavailable, case_live_machine_wording):
            try:
                fn()
            except Exception as error:  # noqa: BLE001 — проба называет, а не падает
                report(False, f"{fn.__name__}: случай упал", [f"{type(error).__name__}: {error}"])
        try:
            case_watch(load_dispatcher_probe())
        except Exception as error:  # noqa: BLE001
            report(False, "часть 2: случай упал", [f"{type(error).__name__}: {error}"])
        if LIVE:
            case_live()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"ЗАКОН БИБЛИОТЕКИ ПРОЦЕССОВ: путей {paths}, неудач {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
