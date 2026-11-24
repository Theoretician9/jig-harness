#!/usr/bin/env python3
"""Постановку обновления ведёт НОВАЯ версия обновлятора.

Находка копии сети 14.11.2026: 0.12.1 не вставал — ворота «до»
звал СТАРЫЙ scripts/update.py копии (предел 1800 с, вывод в буфер), и починки
0.12.1 до этого места не доходили: новый код ещё не стоял.

Стенд — свой каталог proba-update-relaunch-*: публичная сборка (голый
репозиторий) с двумя коммитами, пакет-клон и копия-проект. Внешнее (sudo,
systemctl) — заглушки первыми в PATH, раскладчик и ворота — заглушки стенда.
Обновлятор берётся РЯДОМ с пробой: из копии дерева проба судит мутанта.

Случаи:
  Р1 копия зовёт свой update.py прежним путём — ворота гонит обновлятор новой
     версии, метка перезапуска доходит до ворот, релиз встал;
  Р2 команда перехода (КОМАНДА_ПЕРЕХОДА) ставит новый релиз на копии, чей
     update.py перезапускаться не умеет;
  Р3 метка в окружении — второго перезапуска нет; обновлятор не менялся —
     перезапуска нет;
  Р4 предел ворот — ключ harness.conf, ход ворот виден в журнале, пока они идут,
     оборванные пределом ворота убиты вместе с детьми.

Прогон: python3 scripts/test_update_relaunch.py   (0 — все случаи прошли)
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPDATER = HERE / "update.py"
LIB = HERE / "lib"
# Конфиги лежат иначе в проекте (harness/config) и в пакете (config рядом со scripts).
CONFIG = next(p for p in (HERE.parent / "harness" / "config", HERE.parent / "config") if p.is_dir())
CONFIG_FILES = ("process-library.json", "process-library.yaml", "system-jobs.yaml")
NEW_MARK = "[обновление] ворота идут ("
B_MARK = "[обновление] ворота идут [версия Б] ("
RELAUNCH_LINE = "постановку ведёт обновлятор новой версии"
RELAUNCH_CALL = "        _перезапуститься_новой_версией(argv)\n"

случаев = неудач = 0


def судить(имя: str, беды: list) -> None:
    global случаев, неудач
    случаев += 1
    if беды:
        неудач += 1
        print(f"ПЛОХО · {имя}")
        for беда in беды:
            print(f"    {беда}")
    else:
        print(f"ок  · {имя}")


def гит(*доводы, где):
    готово = subprocess.run(["git", *доводы], cwd=str(где), capture_output=True, text=True, timeout=60)
    if готово.returncode != 0:
        raise RuntimeError(f"git {' '.join(доводы)}: {готово.stderr.strip()}")
    return готово.stdout.strip()


def заменить_раз(текст: str, было: str, стало: str) -> str:
    """Версия стенда — обновлятор пробы с одной правкой; не нашлась ровно одна — стенд неверен."""
    if текст.count(было) != 1:
        raise RuntimeError(f"стенд: «{было.strip()}» встречается {текст.count(было)} раз, ждали 1")
    return текст.replace(было, стало)


def новый_текст() -> str:
    return UPDATER.read_text(encoding="utf-8")


def версия_б() -> str:
    """Новый релиз: обновлятор пробы, чьи ворота узнаются по метке «[версия Б]»."""
    return заменить_раз(новый_текст(), NEW_MARK, B_MARK)


def старый_текст() -> str:
    """Обновлятор копии, который перезапускаться не умеет."""
    return заменить_раз(новый_текст(), RELAUNCH_CALL, "        pass\n")


LAYOUT_STUB = """#!/bin/bash
# Раскладчик стенда: кладёт обновлятор пакета в проект, как настоящий.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/scripts/update.py" "$STAND_PROJECT/scripts/update.py"
cp -r "$here/scripts/lib/." "$STAND_PROJECT/scripts/lib/"
"""


def положить_релиз(исток: Path, обновлятор: str, метка: str) -> str:
    скрипты = исток / "harness" / "scripts"
    скрипты.mkdir(parents=True, exist_ok=True)
    (скрипты / "update.py").write_text(обновлятор, encoding="utf-8")
    if not (скрипты / "lib").exists():
        shutil.copytree(LIB, скрипты / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        конф = исток / "harness" / "config"
        конф.mkdir(parents=True)
        for имя in CONFIG_FILES:
            shutil.copy2(CONFIG / имя, конф / имя)
        раскладчик = исток / "harness" / "razlozhit-harnes.sh"
        раскладчик.write_text(LAYOUT_STUB, encoding="utf-8")
        раскладчик.chmod(0o755)
    (исток / "harness" / "версия.txt").write_text(метка + "\n", encoding="utf-8")
    гит("add", "-A", где=исток)
    гит("commit", "-q", "-m", f"релиз {метка}", где=исток)
    гит("push", "-q", "origin", "master", где=исток)
    return гит("rev-parse", "HEAD", где=исток)


class Стенд:
    """Публичная сборка из двух коммитов, пакет на первом, копия со своим обновлятором."""

    def __init__(self, *, у_копии: str, релиз_а: str, релиз_б: str, предел: int = 120):
        self.корень = Path(tempfile.mkdtemp(prefix="proba-update-relaunch-"))
        к = self.корень
        публичный = к / "public.git"
        гит("init", "-q", "--bare", "-b", "master", str(публичный), где=к)
        исток = к / "src"
        гит("clone", "-q", str(публичный), str(исток), где=к)
        гит("config", "user.email", "proba@harness", где=исток)
        гит("config", "user.name", "proba", где=исток)
        self.коммит_а = положить_релиз(исток, релиз_а, "А")
        self.пакет = к / "package"
        гит("clone", "-q", str(публичный), str(self.пакет), где=к)
        self.коммит_б = положить_релиз(исток, релиз_б, "Б")

        self.проект = к / "project"
        скрипты = self.проект / "scripts"
        скрипты.mkdir(parents=True)
        (скрипты / "update.py").write_text(у_копии, encoding="utf-8")
        shutil.copytree(LIB, скрипты / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        self.вызовы_ворот = к / "gates-calls"
        self.без_итога = к / "gates-no-total"
        (скрипты / "gates.sh").write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' \"${{HARNESS_UPDATE_RELAUNCHED:-нет}}\" >> '{self.вызовы_ворот}'\n"
            "echo '=== ворота стенда ==='\n"
            "echo '  ок · шаг первый'\n"
            f"sleep \"${{STAND_GATES_SLEEP:-0}}\" & echo $! > '{к / 'gates-sleep.pid'}'; wait\n"
            f"cat '{к / 'gates-extra'}' 2>/dev/null\n"
            f"[ -f '{self.без_итога}' ] || {{ echo '=== итог: зелёных 1, красных 0 ==='; "
            f"python3 '{к / 'journal_gates.py'}'; }}\n",
            encoding="utf-8")
        # Журналы, как пишет настоящий gates.sh: проверки — в GATES_TIMING_DIR или LOG_DIR,
        # итог — в LOG_DIR. Красные — строки «  ПЛОХО · имя» из gates-extra.
        (к / "journal_gates.py").write_text(f"""import json, os, re
from datetime import datetime
from pathlib import Path
журналы = Path({str(к / 'log')!r})
сейчас = datetime.now().astimezone().isoformat(timespec="seconds")
прогон = сейчас + " " + str(os.getpid())
доп = Path({str(к / 'gates-extra')!r})
красные = re.findall(r"^  ПЛОХО · (\\S+)", доп.read_text(encoding="utf-8"), re.M) if доп.is_file() else []
место = Path(os.environ.get("GATES_TIMING_DIR") or журналы)
with open(место / "gates-timing.jsonl", "a", encoding="utf-8") as ж:
    for имя in ["секреты", *красные]:
        ж.write(json.dumps({{"v": 1, "ts": сейчас, "event": "gate.check",
                            "outcome": "refused" if имя in красные else "ok",
                            "data": {{"name": имя, "run": прогон}}}}, ensure_ascii=False) + "\\n")
with open(журналы / "ворота-итог.jsonl", "a", encoding="utf-8") as ж:
    ж.write(json.dumps({{"ts": сейчас, "режим": "обычный", "зелёных": 1, "красных": len(красные)}},
                       ensure_ascii=False) + "\\n")
""", encoding="utf-8")
        (скрипты / "install-new.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        конф_проекта = self.проект / "harness" / "config"
        конф_проекта.mkdir(parents=True)
        (конф_проекта / "mechanisms.yaml").write_text("механизмы: []\n", encoding="utf-8")
        for имя in CONFIG_FILES:
            shutil.copy2(CONFIG / имя, конф_проекта / имя)

        дом = к / "home"
        установка = дом / ".harness"
        установка.mkdir(parents=True)
        self.журналы = к / "log"
        (self.журналы / "locks").mkdir(parents=True)
        (установка / "install.conf").write_text(
            f'PROJECT_DIR="{self.проект}"\nPROJECT_NAME="стенд"\nAGENT_USER="proba"\n'
            f'LOG_DIR="{self.журналы}"\nSTATE_DIR="{к / "state"}"\n', encoding="utf-8")
        (установка / "harness.conf").write_text(
            f'STARTER_DIR="{self.пакет}"\nBACKUP_DIR="{к / "backups"}"\n'
            f'UPDATE_GATES_TIMEOUT_SEC="{предел}"\n', encoding="utf-8")
        заглушки = к / "bin"
        заглушки.mkdir()
        for имя in ("sudo", "systemctl"):
            (заглушки / имя).write_text(
                f"#!/bin/bash\nprintf '%s\\n' \"$(basename \"$0\") $*\" >> '{к / 'external-calls'}'\nexit 1\n",
                encoding="utf-8")
            (заглушки / имя).chmod(0o755)
        (к / "tmp").mkdir()
        self.версия = к / "version.json"
        self.окружение = {
            "PATH": f"{заглушки}:/usr/local/bin:/usr/bin:/bin", "HOME": str(дом),
            "HARNESS_HOME": str(установка), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HARNESS_TG_SEND": "", "HARNESS_ALERT_TASK": "/bin/true",
            "HARNESS_VERSION_FILE": str(self.версия), "TMPDIR": str(к / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1", "STAND_PROJECT": str(self.проект)}

    def пустить(self, команда: list, **ещё) -> subprocess.CompletedProcess:
        return subprocess.run(команда, cwd=str(self.проект), env={**self.окружение, **ещё},
                              capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)

    def поставить(self, **ещё) -> subprocess.CompletedProcess:
        return self.пустить([sys.executable, str(self.проект / "scripts" / "update.py"), "--поставить"], **ещё)

    def записать_основу(self, красные=(), *, давность=timedelta(hours=3), режим="обычный") -> None:
        """Прошлый законченный полный прогон ворот установки: проверки и итог за ними."""
        конец = datetime.now().astimezone() - давность
        прогон = f"{(конец - timedelta(minutes=30)).isoformat(timespec='seconds')} 4242"
        with open(self.журналы / "gates-timing.jsonl", "a", encoding="utf-8") as журнал:
            for имя in ("секреты", "гигиена", *красные):
                журнал.write(json.dumps({
                    "v": 1, "ts": конец.isoformat(timespec="seconds"), "src": "scripts/gates.sh",
                    "event": "gate.check", "outcome": "refused" if имя in красные else "ok",
                    "data": {"name": имя, "run": прогон, "secs": 1.0, "rc": 1 if имя in красные else 0}},
                    ensure_ascii=False) + "\n")
        with open(self.журналы / "ворота-итог.jsonl", "a", encoding="utf-8") as итог:
            итог.write(json.dumps({"ts": (конец + timedelta(seconds=3)).isoformat(timespec="seconds"),
                                   "режим": режим, "зелёных": 2, "красных": len(красные)},
                                  ensure_ascii=False) + "\n")

    def ворота_дадут(self, *строки) -> None:
        (self.корень / "gates-extra").write_text("".join(f"{с}\n" for с in строки), encoding="utf-8")

    def ворота_звали(self) -> list:
        return (self.вызовы_ворот.read_text(encoding="utf-8").split()
                if self.вызовы_ворот.is_file() else [])

    def версия_записана(self) -> str:
        return self.версия.read_text(encoding="utf-8") if self.версия.is_file() else ""

    def пакет_на(self) -> str:
        return гит("rev-parse", "HEAD", где=self.пакет)

    def убрать(self) -> None:
        shutil.rmtree(self.корень, ignore_errors=True)


def вывод(готово) -> str:
    return (готово.stdout or "") + (готово.stderr or "")


def хвост(готово) -> str:
    return " | ".join(вывод(готово).strip().splitlines()[-6:])


def извлечения_остались(стенд) -> list:
    return sorted(п.name for п in (стенд.корень / "tmp").iterdir())


def случай_р1():
    """Копия на А зовёт свой update.py; в публичной Б — ворота гонит обновлятор Б."""
    стенд = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=версия_б())
    try:
        готово = стенд.поставить()
        текст, беды = вывод(готово), []
        if B_MARK not in текст:
            беды.append(f"ворота гнал не обновлятор новой версии (метки «[версия Б]» нет): {хвост(готово)}")
        if текст.count(RELAUNCH_LINE) != 1:
            беды.append(f"строк о перезапуске {текст.count(RELAUNCH_LINE)}, ждали 1")
        звали = стенд.ворота_звали()
        if звали != [стенд.коммит_б] * 2:
            беды.append(f"ворота «до» и «после» без метки коммита Б: {звали}")
        if готово.returncode != 0 or стенд.коммит_б not in стенд.версия_записана():
            беды.append(f"релиз Б не встал (код {готово.returncode}): {хвост(готово)}")
        if стенд.пакет_на() != стенд.коммит_б:
            беды.append("пакет не на коммите Б")
        if (стенд.проект / "scripts" / "update.py").read_text(encoding="utf-8") != версия_б():
            беды.append("в проекте не обновлятор Б")
        if извлечения_остались(стенд):
            беды.append(f"извлечение обновлятора не убрано: {извлечения_остались(стенд)}")
        return беды
    finally:
        стенд.убрать()


def команда_перехода() -> str:
    спец = importlib.util.spec_from_file_location("обновление_пробы", UPDATER)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль.КОМАНДА_ПЕРЕХОДА


def случай_р2():
    """У копии обновлятор без перезапуска; команда перехода ставит релиз Б его обновлятором."""
    стенд = Стенд(у_копии=старый_текст(), релиз_а=старый_текст(), релиз_б=версия_б())
    try:
        готово = стенд.пустить(["bash", "-c", команда_перехода().format(проект=стенд.проект)])
        текст, беды = вывод(готово), []
        if готово.returncode != 0 or стенд.коммит_б not in стенд.версия_записана():
            беды.append(f"команда перехода не поставила релиз Б (код {готово.returncode}): {хвост(готово)}")
        if B_MARK not in текст or NEW_MARK in текст:
            беды.append(f"ворота гнал не обновлятор Б: {хвост(готово)}")
        if стенд.пакет_на() != стенд.коммит_б:
            беды.append("пакет не на коммите Б")
        if (стенд.проект / "scripts" / "update.py").read_text(encoding="utf-8") != версия_б():
            беды.append("в проекте не обновлятор Б — следующее обновление снова поведёт старый")
        if извлечения_остались(стенд):
            беды.append(f"извлечение команды перехода не убрано: {извлечения_остались(стенд)}")
        return беды
    finally:
        стенд.убрать()


def случай_р2_без_поддержки():
    """В публичной сборке обновлятор без перезапуска — команда перехода отказывает, пакет не тронут."""
    # Релиз до договора перезапуска (как 0.12.1): имени метки в его обновляторе нет.
    до_договора = старый_текст().replace("HARNESS_UPDATE_RELAUNCHED", "HARNESS_UPDATE_NONE")
    стенд = Стенд(у_копии=старый_текст(), релиз_а=старый_текст(), релиз_б=до_договора)
    try:
        готово = стенд.пустить(["bash", "-c", команда_перехода().format(проект=стенд.проект)])
        беды = []
        if готово.returncode == 0:
            беды.append("команда перехода прошла на релизе без перезапуска")
        if "ещё нет обновлятора" not in вывод(готово):
            беды.append(f"отказ не назван словами: {хвост(готово)}")
        if стенд.пакет_на() != стенд.коммит_а or стенд.ворота_звали():
            беды.append("пакет тронут или ворота звались")
        return беды
    finally:
        стенд.убрать()


def случай_р3_метка():
    """Метка перезапуска в окружении — обновлятор копии ведёт сам, второго exec нет."""
    стенд = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=версия_б())
    try:
        стенд.без_итога.write_text("", encoding="utf-8")
        готово = стенд.поставить(HARNESS_UPDATE_RELAUNCHED="уже")
        текст, беды = вывод(готово), []
        if RELAUNCH_LINE in текст or B_MARK in текст:
            беды.append(f"при метке в окружении обновлятор перезапустился: {хвост(готово)}")
        if NEW_MARK not in текст:
            беды.append(f"ворота копии не шли: {хвост(готово)}")
        return беды
    finally:
        стенд.убрать()


def случай_р3_тот_же():
    """Новый релиз с тем же обновлятором — перезапуска нет."""
    стенд = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=новый_текст())
    try:
        стенд.без_итога.write_text("", encoding="utf-8")
        готово = стенд.поставить()
        беды = []
        if RELAUNCH_LINE in вывод(готово):
            беды.append(f"обновлятор не менялся, а перезапуск был: {хвост(готово)}")
        if стенд.ворота_звали() != ["нет"]:
            беды.append(f"ворота шли не одним прогоном без метки: {стенд.ворота_звали()}")
        return беды
    finally:
        стенд.убрать()


def живой(файл_pid: Path) -> bool:
    """Процесс из файла жив и не зомби."""
    try:
        стат = Path(f"/proc/{int(файл_pid.read_text())}/stat").read_text()
    except (OSError, ValueError):
        return False
    return стат.rsplit(")", 1)[1].split()[0] != "Z"


def добить(файл_pid: Path) -> None:
    if живой(файл_pid):
        os.kill(int(файл_pid.read_text()), 9)


def случай_р4():
    """Предел из harness.conf, ход ворот виден, пока идут; по пределу убита вся группа."""
    стенд = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=версия_б(), предел=4)
    try:
        журнал = стенд.журналы / "update-gates.log"
        процесс = subprocess.Popen(
            [sys.executable, str(стенд.проект / "scripts" / "update.py"), "--поставить"],
            cwd=str(стенд.проект), env={**стенд.окружение, "STAND_GATES_SLEEP": "40"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL)
        начало, виден_ход = time.monotonic(), False
        while процесс.poll() is None and time.monotonic() - начало < 60:
            if журнал.is_file() and "шаг первый" in журнал.read_text(encoding="utf-8", errors="replace"):
                виден_ход = True
            time.sleep(0.2)
        if процесс.poll() is None:
            процесс.kill()
        текст = процесс.communicate()[0]
        прошло = time.monotonic() - начало
        беды = []
        if not виден_ход:
            беды.append(f"в журнале {журнал.name} хода ворот не было, пока они шли")
        if "предел 4 с" not in текст or "UPDATE_GATES_TIMEOUT_SEC" not in текст:
            беды.append(f"отказ не называет предел из harness.conf: {' | '.join(текст.strip().splitlines()[-4:])}")
        if прошло > 30:
            беды.append(f"ворота не оборваны пределом 4 с: шли {прошло:.0f} с")
        time.sleep(0.5)
        if живой(стенд.корень / "gates-sleep.pid"):
            беды.append("дети оборванных ворот живы: sleep стенда пережил предел")
        if стенд.пакет_на() != стенд.коммит_а:
            беды.append("ворота «до» оборваны, а пакет тронут")
        return беды
    finally:
        добить(стенд.корень / "gates-sleep.pid")
        стенд.убрать()


def поставить_за(стенд, сон: int) -> tuple:
    начало = time.monotonic()
    готово = стенд.поставить(STAND_GATES_SLEEP=str(сон))
    return готово, time.monotonic() - начало


def случай_р5():
    """Есть свежий полный прогон: до обновления gates.sh не зовётся, после — ровно раз.
    Замер: ворота стенда по 3 с — постановка с основой против постановки без неё."""
    с_основой = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=версия_б())
    без_основы = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=версия_б())
    try:
        с_основой.записать_основу(красные=("среда",))
        готово, быстро = поставить_за(с_основой, 3)
        _, долго = поставить_за(без_основы, 3)
        беды = []
        if с_основой.ворота_звали() != [с_основой.коммит_б]:
            беды.append(f"gates.sh звался {len(с_основой.ворота_звали())} раз, ждали один — после")
        if готово.returncode != 0 or с_основой.коммит_б not in с_основой.версия_записана():
            беды.append(f"релиз не встал на основе прошлого прогона: {хвост(готово)}")
        if "основа — ворота" not in вывод(готово):
            беды.append("журнал постановки не называет основу")
        if len(без_основы.ворота_звали()) != 2:
            беды.append(f"без основы ворота до обновления не шли: {без_основы.ворота_звали()}")
        print(f"    замер: ворота стенда 3 с — с основой {быстро:.1f} с (прогонов "
              f"{len(с_основой.ворота_звали())}), без основы {долго:.1f} с (прогонов "
              f"{len(без_основы.ворота_звали())})")
        return беды
    finally:
        с_основой.убрать()
        без_основы.убрать()


def случай_р6():
    """Новая красная после — полный откат; вторая попытка не берёт откаченный прогон основой."""
    стенд = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=версия_б())
    try:
        стенд.записать_основу(красные=("среда",))
        стенд.ворота_дадут("  ПЛОХО · среда", "  ПЛОХО · новая")
        беды = []
        for попытка in (1, 2):
            готово = стенд.поставить()
            if готово.returncode == 0 or стенд.версия_записана():
                беды.append(f"попытка {попытка}: новая красная, а релиз встал: {хвост(готово)}")
            if стенд.пакет_на() != стенд.коммит_а:
                беды.append(f"попытка {попытка}: пакет не откачен на А")
            if (стенд.проект / "scripts" / "update.py").read_text(encoding="utf-8") != новый_текст():
                беды.append(f"попытка {попытка}: в проекте не прежний обновлятор")
            if len(стенд.ворота_звали()) != попытка:
                беды.append(f"попытка {попытка}: прогонов ворот {len(стенд.ворота_звали())}, ждали {попытка}")
        return беды
    finally:
        стенд.убрать()


def случай_р7():
    """Красная, что была в основе, — не новая: релиз встаёт. Основа — ПОСЛЕДНИЙ прогон:
    прогон двухдневной давности без красных её не подменяет."""
    стенд = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=версия_б())
    try:
        стенд.записать_основу(давность=timedelta(days=2))
        стенд.записать_основу(красные=("среда",))
        стенд.ворота_дадут("  ПЛОХО · среда")
        готово = стенд.поставить()
        if готово.returncode != 0 or стенд.коммит_б not in стенд.версия_записана():
            return [f"красная основы прочтена новой — откат: {хвост(готово)}"]
        return []
    finally:
        стенд.убрать()


def случай_р8():
    """Основы нет годной — старая (8 суток), выборочная или оборванная: ворота идут до обновления."""
    беды = []
    for что, запись in (("8 суток", {"давность": timedelta(days=8)}),
                        ("хакатон", {"режим": "хакатон"}),
                        ("оборванный", None)):
        стенд = Стенд(у_копии=новый_текст(), релиз_а=новый_текст(), релиз_б=версия_б())
        try:
            if запись is None:
                стенд.записать_основу()
                (стенд.журналы / "ворота-итог.jsonl").unlink()
            else:
                стенд.записать_основу(**запись)
            стенд.поставить()
            if len(стенд.ворота_звали()) != 2:
                беды.append(f"{что}: прогон принят основой, ворот до обновления не было")
        finally:
            стенд.убрать()
    return беды


СЛУЧАИ = (
    ("Р1 update.py копии прежним путём: ворота «до» и «после» гонит обновлятор новой версии, релиз встал",
     случай_р1),
    ("Р2 команда перехода ставит релиз на копии, чей update.py перезапускаться не умеет", случай_р2),
    ("Р2б команда перехода на релизе без перезапуска: отказ словами, пакет не тронут", случай_р2_без_поддержки),
    ("Р3 метка перезапуска в окружении: второго перезапуска нет", случай_р3_метка),
    ("Р3б обновлятор в релизе не менялся: перезапуска нет", случай_р3_тот_же),
    ("Р4 предел ворот — ключ harness.conf, ход в журнале виден, по пределу убита группа", случай_р4),
    ("Р5 основа — прошлый полный прогон: до обновления gates.sh не зовётся, после — один раз", случай_р5),
    ("Р6 новая красная после — полный откат, и вторая попытка тоже: откаченный прогон не основа",
     случай_р6),
    ("Р7 красная, что была в основе, не новая — релиз встаёт", случай_р7),
    ("Р8 основа старше 7 суток, выборочная или оборванная — ворота идут до обновления", случай_р8),
)


def main() -> int:
    выбор = sys.argv[sys.argv.index("--случай") + 1] if "--случай" in sys.argv else ""
    for имя, случай in СЛУЧАИ:
        if выбор and not имя.startswith(выбор + " "):
            continue
        try:
            беды = случай()
        except Exception as беда:  # noqa: BLE001 — сломанный стенд тоже красный случай
            беды = [f"исключение {type(беда).__name__}: {беда}"]
        судить(имя, беды)
    print(f"ОБНОВЛЕНИЕ НОВОЙ ВЕРСИЕЙ: случаев {случаев}, неудач {неудач}")
    return 1 if неудач or not случаев else 0


if __name__ == "__main__":
    sys.exit(main())
