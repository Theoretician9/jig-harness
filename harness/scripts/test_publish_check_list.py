#!/usr/bin/env python3
"""Пробы чек-листа выпуска (`publish.sh --чек-лист`) — до кода.

Слово владельца 24.11.2026: «чтобы релизы проще и быстрее выпускались, чтобы эти ворота
не тормозили а сразу все что надо по ним как по чек листу все было сделано». Выпуск
0.15.0 шёл 5 кругов ворот поезда по ~45 мин, и каждый круг ловил ОДНУ мелочь, видную
за минуты до ворот. Договор — спека задачи (каталог задачи в .tasks, имя — в трейлере Dev-Map коммита):

* `--чек-лист` прогоняет быстрые замки — заметки и номер (6в), секреты (5), чистоту
  сборки (6) с обезличенным прошлым CHANGELOG, заметки релиза (6б), сроки карантина,
  непроверенные статусы и запись пропуска (3), согласие (2) — и печатает ВСЕ находки
  одним списком, не останавливаясь на первой; строка итога
  «=== чек-лист выпуска: находок N, секунд S ===», находки — под ней; код 0 — чисто, 1 — находки.
* Ворот (gates.sh) и типовой копии (6г) не зовёт, в канал не пишет, общий каталог
  сборки не трогает.
* `--поезд` и `--проверка` зовут его первым шагом: при находках — отказ до среза и ворот.

Каждый случай строит свой двор: пакет, удалённый с публичной веткой, проект с картой и
трейлерами, журнал, стенд с КОПИЕЙ publish.sh (дверь паспорта — HARNESS_DOOR_STAND_HOME).
Боевые данные не читаются. Дверь мутации — PUBLISH_UNDER_TEST (копия publish.sh).

Прогон (только фоном, через пул): bash scripts/heavy-slot.sh -- python3 scripts/test_publish_check_list.py [префикс случая…]
Итог: «ЧЕК-ЛИСТ ВЫПУСКА: случаев N, неудач M», код 0/1.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PUBLISH = Path(os.environ.get("PUBLISH_UNDER_TEST") or HERE / "publish.sh")
# Судьи, которых чек-лист зовёт рядом с собой (SCRIPTS_REAL стенда): настоящие, ссылкой.
LINKED = ("build-public.py", "version.py", "check-release-notes.py", "check-public-clean.py",
          "gates-quarantine.py", "vitrina_status.py", "service-only-diff.py")
CARD_DONE = "hr.ck-done"
CARD_TEST = "hr.ck-test"
CARD_QUAR = "hr.ck-quarantine"
SUMMARY = re.compile(r"=== чек-лист выпуска: находок (\d+), секунд (\d+) ===")
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(where: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(where), *GIT_ID, *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=60)
    assert proc.returncode == 0, f"git {' '.join(args)} в {where}: {proc.stderr}"
    return proc.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def notes(title: str) -> str:
    return (f"# Что нового\n\n## {date.today().isoformat()} — {title}\n\n### Исправлено\n\n"
            "- Сессия больше не обрывается.\n\n### Что изменилось для вас\n\n- Ничего делать не нужно.\n")


class Yard:
    """Двор одного случая. Чистый по умолчанию: каждая порча — свой метод."""

    def __init__(self, root: Path):
        self.d = root
        self.package, self.build = root / "пакет", root / "сборка"
        self.project, self.log, self.stand = root / "проект", root / "лог", root / "стенд"
        self.remote = root / "удалённый.git"
        self.build_before = None
        self._package()
        self._public_branch()
        self._project()
        self._stand()

    def _package(self):
        p = self.package
        write(p / "harness/CLAUDE.md", "# харнес\n@STATE.md\n")
        write(p / "harness/STATE.md", "состояние\n")
        write(p / "harness/self/core.json", "{}\n")
        write(p / "harness/память/TONE.md", "# ТОН\nзамер речи\n")
        write(p / "harness/шаблоны-задач/ТОН-заготовка.md", "# ТОН\n\n<!-- потолки: мягкий=700 жёсткий=1500 -->\n")
        write(p / "harness/config/public-build.yaml",
              "исключить: []\nзаменить_шаблоном:\n  harness/память/TONE.md: harness/шаблоны-задач/ТОН-заготовка.md\n"
              "обезличить_ключи: [PROJECT_DIR]\nоставить: [harness/CLAUDE.md]\n")
        write(p / "ЧТО-НОВОГО.md", notes("Выпуск 0.1.0: первый"))
        git(self.d, "init", "-q", "-b", "master", str(p))
        git(p, "add", "-A")
        git(p, "commit", "-qm", "проба")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        git(p, "remote", "add", "jig", str(self.remote))

    def _public_branch(self):
        # Публичная ветка без тегов: точка отсечки замка 3 — её дата, давняя.
        seed = self.d / "прошлое"
        write(seed / "README.md", "прошлый выпуск\n")
        git(self.d, "init", "-q", "-b", "master", str(seed))
        git(seed, "add", "-A")
        old = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+05:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+05:00"}
        git(seed, "commit", "-qm", "прошлый выпуск", env=old)
        git(seed, "push", "-q", str(self.remote), "HEAD:master")

    def _project(self):
        p = self.project
        self.devmap(test_status="done")
        write(p / "harness/config/gates-quarantine.yaml", "карантин: []\n")
        git(self.d, "init", "-q", "-b", "master", str(p))
        git(p, "add", "-A")
        git(p, "commit", "-qm", "карта")
        git(p, "commit", "-q", "--allow-empty", "-m", f"задача\n\nDev-Map: {CARD_DONE}")
        git(p, "commit", "-q", "--allow-empty", "-m", f"задача вторая\n\nDev-Map: {CARD_TEST}")
        self.log.mkdir()

    def devmap(self, test_status: str):
        write(self.project / "dev-map.yaml",
              "version: 1\nmilestones:\n  - id: ms.ck\nlines:\n  - id: ln.ck\n    tasks:\n"
              f"      - id: {CARD_DONE}\n        status: done\n        title: проверенная\n"
              f"      - id: {CARD_TEST}\n        status: {test_status}\n        title: вторая\n"
              f"      - id: {CARD_QUAR}\n        status: plan\n        title: карантинная\n")

    def _stand(self):
        s = self.stand / "scripts"
        s.mkdir(parents=True)
        (s / "publish.sh").write_bytes(PUBLISH.read_bytes())
        (s / "lib").symlink_to(HERE / "lib")
        (s / "check-secrets.sh").write_bytes((HERE / "check-secrets.sh").read_bytes())
        for name in LINKED:
            (s / name).symlink_to(HERE / name)
        # Подставные двери: согласие — код из файла двора; ворота, канал и типовая копия — метки
        # «звали»: чек-лист их звать не должен.
        write(s / "deploy_guard.py",
              "import pathlib, sys\nd = pathlib.Path(%r)\n" % str(self.d)
              + "(d / 'согласие-звали').open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
              "rc = int((d / 'согласие-код').read_text()) if (d / 'согласие-код').exists() else 0\n"
              "print('согласия нет (подставной судья)' if rc else 'предмет: подставной')\nsys.exit(rc)\n")
        for name in ("gates.sh", "tg_send.sh"):
            write(s / name, f'#!/bin/sh\necho "{name} $*" >> "{self.d}/звали"\n')
        write(s / "check-typical-copy.py", f'open("{self.d}/звали", "a").write("типовая\\n")\n')
        write(self.stand / "install.conf",
              f'PROJECT_DIR="{self.project}"\nAGENT_USER="подставной"\nLOG_DIR="{self.log}"\n'
              f'PROJECT_NAME="Подставной"\nTG_CHAT_ID="100000001"\nPRODUCT_DIR="tovar"\n'
              f'STARTER_DIR="{self.package}"\n')
        write(self.stand / "harness.conf",
              f'PUBLIC_REMOTE="jig"\nPUBLIC_BRANCH="master"\nPUBLIC_BUILD_DIR="{self.build}"\n'
              'PUBLIC_COMMIT_NAME="проба-харнес"\nPUBLIC_COMMIT_EMAIL="proba@harness.local"\n'
              'MERGE_QUEUE_WAIT_MIN="1"\n')

    # ── порча: одна находка на метод ──
    def package_commit(self, rel: str, text: str, title: str = "правка"):
        write(self.package / rel, text)
        git(self.package, "add", "-A")
        git(self.package, "commit", "-qm", title)

    def spoil_notes_number(self):
        self.package_commit("ЧТО-НОВОГО.md", notes("Выпуск 0.2.0: не тот номер"))

    def spoil_secret(self):
        shape = 'TOKEN = "%s:AA%s"'  # не-секрет: образец формы, собирается из кусков
        self.package_commit("harness/тайна.py", shape % ("12345678", "a" * 33) + "\n")

    def spoil_trace(self):
        self.package_commit("harness/след.py", f'ПУТЬ = "{self.project}"\n')

    def spoil_release_title(self):
        # Прошлая секция CHANGELOG публичной ветки называет установку. Сборщик сносит файл из дерева
        # (замок 6 чист), version.py --оформить возвращает его из HEAD сборки — ловит только 6б.
        # Заголовок нового коммита для этого не годится: version.py сам снимает строки со следом
        # по своему паспорту, и проба зеленела, лишь пока он читал паспорт машины, а не стенда.
        git(self.d, "clone", "-q", "-o", "jig", str(self.remote), str(self.build))
        write(self.build / "CHANGELOG.md", "# Журнал изменений\n\n## [0.0.9] — 2026-01-01\n\n- правка под Подставной\n")
        old = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+05:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+05:00"}
        git(self.build, "add", "-A")
        git(self.build, "commit", "-qm", "прошлый выпуск с журналом", env=old)
        git(self.build, "push", "-q", "jig", "HEAD:master")
        write(self.build / ".публичная-сборка", "")  # метка: сборщик пишет только в свой каталог
        # Файла на диске нет, как после rsync --delete сборщика: журнал живёт только в HEAD сборки.
        (self.build / "CHANGELOG.md").unlink()
        # FETCH_HEAD до прогона: fetch точки отсечки иначе добавит файл, и «тихо» судило бы его.
        git(self.build, "fetch", "-q", "jig")

    def spoil_quarantine(self):
        today = date.today()
        write(self.project / "harness/config/gates-quarantine.yaml",
              f'карантин:\n  - гейт: "истекающая"\n    карточка: {CARD_QUAR}\n'
              f'    заведено: "{(today - timedelta(days=2)).isoformat()}"\n    срок: "{today.isoformat()}"\n'
              '    причина: "проба"\n')

    def spoil_status(self):
        self.devmap(test_status="test")

    def waiver(self, until_sec: int):
        now = int(time.time())
        write(self.log / "publish-gates-waiver.json", json.dumps(
            {"слово": "без прогона полного", "id_входящего": "1793351076.532892567", "причина": "проба",
             "выдана": now - 60, "до": now + until_sec}, ensure_ascii=False))

    def refuse_consent(self):
        write(self.d / "согласие-код", "1\n")

    def run(self, *args: str):
        self.build_before = self.build_files()
        tmp = self.d / "tmp"
        tmp.mkdir(exist_ok=True)
        env = {**os.environ, "TMPDIR": str(tmp), "HARNESS_DOOR_STAND_HOME": str(self.stand)}
        for key in ("PROJECT_DIR", "HARNESS_STARTER", "LOG_DIR", "HARNESS_LOG_DIR"):
            env.pop(key, None)
        t0 = time.time()
        proc = subprocess.run(["bash", str(self.stand / "scripts/publish.sh"), *args], capture_output=True,
                              text=True, env=env, timeout=600)
        return proc.returncode, proc.stdout + proc.stderr, time.time() - t0

    def build_files(self):
        if not self.build.exists():
            return None
        return sorted(p.relative_to(self.build).as_posix() for p in self.build.rglob("*"))

    def called(self) -> str:
        f = self.d / "звали"
        return f.read_text(encoding="utf-8") if f.exists() else ""


def findings(out: str) -> int:
    m = SUMMARY.search(out)
    assert m, f"нет строки «=== чек-лист выпуска: находок N, секунд S ===»:\n{out}"
    return int(m.group(1))


def finding_list(out: str) -> str:
    """Находки печатаются ПОД строкой итога — одним списком."""
    return out[SUMMARY.search(out).end():]


def assert_quiet(y: Yard, out: str):
    called = y.called()
    assert "gates.sh" not in called, f"чек-лист звал ворота:\n{called}\n{out}"
    assert "tg_send.sh" not in called, f"чек-лист писал в канал:\n{called}\n{out}"
    assert "типовая" not in called, f"чек-лист звал типовую копию (замок 6г):\n{called}\n{out}"
    after = y.build_files()
    assert after == y.build_before, f"чек-лист тронул общий каталог сборки: {y.build_before} → {after}"


def case_clean(root: Path):
    # Зелёным оставит: чек-лист, молча пропускающий замок, — ловит перечень имён замков.
    y = Yard(root)
    rc, out, sec = y.run("--чек-лист")
    assert rc == 0 and findings(out) == 0, f"чистый двор — находки (код {rc}):\n{out}"
    for lock in ("замок 6в", "замок 5", "замок 6 ", "замок 6б", "замок 3", "замок 2", "карантин"):
        assert lock in out, f"«{lock}» не назван:\n{out}"
    assert sec < 120, f"чек-лист шёл {sec:.0f} с — дольше двух минут"
    assert_quiet(y, out)


def one_finding(root: Path, spoil: str, *needles: str):
    y = Yard(root)
    getattr(y, spoil)()
    rc, out, _ = y.run("--чек-лист")
    assert rc == 1, f"{spoil}: код {rc}, а нужна находка:\n{out}"
    assert findings(out) == 1, f"{spoil}: находок не одна:\n{out}"
    tail = finding_list(out)
    for needle in needles:
        assert needle in tail, f"{spoil}: в списке находок нет «{needle}»:\n{tail}"
    assert_quiet(y, out)


def case_notes(root):
    one_finding(root, "spoil_notes_number", "6в", "0.2.0", "0.1.0")


def case_secret(root):
    one_finding(root, "spoil_secret", "замок 5")


def case_trace(root):
    one_finding(root, "spoil_trace", "замок 6", "PROJECT_DIR")


def case_release_title(root):
    one_finding(root, "spoil_release_title", "6б")


def case_quarantine(root):
    one_finding(root, "spoil_quarantine", "карантин", "истекающая", "истекает")


def case_status_no_waiver(root):
    one_finding(root, "spoil_status", "замок 3", CARD_TEST)


def case_status_waiver_ok(root):
    # Годная запись пропуска снимает замок 3 и называет срок: находки нет.
    y = Yard(root)
    y.spoil_status()
    y.waiver(5 * 3600)
    rc, out, _ = y.run("--чек-лист")
    assert rc == 0 and findings(out) == 0, f"годная запись пропуска — а находка (код {rc}):\n{out}"
    assert re.search(r"замок 3 .*до \d\d:\d\d", out), f"срок записи пропуска не назван:\n{out}"
    assert_quiet(y, out)


def case_status_waiver_expiring(root):
    # Запись истекает раньше, чем кончатся ворота поезда: замок 3 упадёт после них — находка сейчас.
    y = Yard(root)
    y.spoil_status()
    y.waiver(600)
    rc, out, _ = y.run("--чек-лист")
    assert rc == 1 and findings(out) == 1, f"запись на 10 минут — не находка (код {rc}):\n{out}"
    assert "замок 3" in finding_list(out), f"находка не про замок 3:\n{out}"


def case_all_at_once(root):
    # Все порчи сразу — все находки одним списком: чек-лист не останавливается на первой.
    y = Yard(root)
    for spoil in ("spoil_notes_number", "spoil_secret", "spoil_trace", "spoil_quarantine", "spoil_status"):
        getattr(y, spoil)()
    rc, out, _ = y.run("--чек-лист")
    assert rc == 1, f"все порчи — код {rc}:\n{out}"
    n = findings(out)
    tail = finding_list(out)
    for needle in ("6в", "замок 5", "замок 6", "истекающая", "замок 3"):
        assert needle in tail, f"в общем списке нет «{needle}» (находок {n}):\n{tail}"
    assert n >= 5, f"находок {n}, а порч пять:\n{tail}"
    assert_quiet(y, out)


def case_check_mode_consent(root):
    # --проверка без поезда зовёт чек-лист первым: согласие для неё — находка, и находки
    # называются все сразу, ворота не зовутся.
    y = Yard(root)
    y.refuse_consent()
    y.spoil_quarantine()
    rc, out, _ = y.run("--проверка")
    assert rc == 1, f"--проверка с находками — код {rc}:\n{out}"
    assert findings(out) == 2, f"--проверка: находок не две (согласие и карантин):\n{out}"
    assert "замок 2" in finding_list(out), f"согласие не в списке находок:\n{out}"
    assert "gates.sh" not in y.called(), f"--проверка звала ворота при находках:\n{y.called()}"


def case_train_refuses_before_slice(root):
    # --поезд с находкой отказывает до среза и ворот; согласие поезду до ворот не находка:
    # кнопку он просит на срез после них.
    y = Yard(root)
    y.refuse_consent()
    y.spoil_notes_number()
    rc, out, _ = y.run("--поезд")
    assert rc == 1 and findings(out) == 1, f"--поезд: код {rc}, находок не одна:\n{out}"
    assert "срез: проект" not in out, f"--поезд резал срез при находках:\n{out}"
    assert "gates.sh" not in y.called(), f"--поезд звал ворота при находках:\n{y.called()}"


CASES = [
    ("а) чистый двор — 0 находок, все замки названы, < 2 мин, тихо", case_clean),
    ("б) заметки: не тот номер (6в)", case_notes),
    ("в) секрет в сборке (5)", case_secret),
    ("г) след установки в сборке (6)", case_trace),
    ("д) след в прошлой секции CHANGELOG (6б)", case_release_title),
    ("е) карантин истекает в ближайшие сутки", case_quarantine),
    ("ж) непроверенный статус без записи пропуска (3)", case_status_no_waiver),
    ("з) годная запись пропуска — срок назван, не находка", case_status_waiver_ok),
    ("и) запись пропуска истекает раньше ворот — находка", case_status_waiver_expiring),
    ("к) все находки сразу — одним списком", case_all_at_once),
    ("л) --проверка: чек-лист первым, согласие — находка", case_check_mode_consent),
    ("м) --поезд: отказ до среза и ворот", case_train_refuses_before_slice),
]


def main() -> int:
    failed = 0
    only = sys.argv[1:]
    cases = [c for c in CASES if not only or any(c[0].startswith(o) for o in only)]
    for title, case in cases:
        with tempfile.TemporaryDirectory(prefix="publish-checklist-probe-") as tmp:
            try:
                case(Path(tmp))
                print(f"  ок    {title}", flush=True)
            except (AssertionError, subprocess.TimeoutExpired) as err:
                failed += 1
                print(f"  ПЛОХО {title}: {err}", flush=True)
    print(f"ЧЕК-ЛИСТ ВЫПУСКА: случаев {len(cases)}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
