#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пробы Ф1 свежести стека: замер, печать, источники, гейт реестра.

Что судится. `stack_freshness.py` обязан назвать, что из стека отстало, и не
имеет права назвать «свежее» то, чего не смог измерить. Источники кормятся
ЗАПИСАННЫМИ ответами (fixtures/stack/, сняты с живой машины), а не выдуманными;
сеть в пробах — ноль обращений, команды машины подменены заглушками в PATH стенда.

Стенд: каждый случай в своём `стек-стенд-*`, окружение процесса собрано с нуля
(не наследуется), поэтому проба не может сыграть на боевых LOG_DIR/STATE_DIR.

Прогон (только фоном):
    python3 scripts/test_stack_freshness.py
"""
import datetime as dt
import http.client
import importlib.util
import re
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ЗДЕСЬ = Path(__file__).resolve().parent
ФИКСТУРЫ = ЗДЕСЬ / "fixtures" / "stack"
ИНСТРУМЕНТ = ЗДЕСЬ / "stack_freshness.py"
ГЕЙТ = ЗДЕСЬ / "check-stack-registry.py"
СЕЙЧАС = "2026-10-25T10:00:00+05:00"
ОБРАЗЕЦ_NODE = "^v?([0-9]+(\\.[0-9]+){1,3})$"
ОБРАЗЕЦ_ЯДРА = "^([0-9]+\\.[0-9]+\\.[0-9]+-[0-9]+)"


def живой_реестр() -> Path:
    # Рабочий каталог — последний кандидат: прибор мутаций зовёт пробу из копии, где есть
    # только scripts/, а реестр — данные дерева, из которого прибор запущен.
    for кандидат in (ЗДЕСЬ.parent / "harness" / "config" / "stack.yaml",
                     ЗДЕСЬ.parent / "config" / "stack.yaml",
                     Path.cwd() / "harness" / "config" / "stack.yaml"):
        if кандидат.is_file():
            return кандидат
    return ЗДЕСЬ.parent / "harness" / "config" / "stack.yaml"


def загрузить_инструмент():
    sys.path.insert(0, str(ЗДЕСЬ))
    спец = importlib.util.spec_from_file_location("stack_freshness", ИНСТРУМЕНТ)
    модуль = importlib.util.module_from_spec(спец)
    sys.modules["stack_freshness"] = модуль     # dataclass ищет свой модуль по имени
    спец.loader.exec_module(модуль)
    return модуль


# ── части стенда ────────────────────────────────────────────────────────────
def часть(имя, источник, текущая=None, **свежая):
    данные = {"имя": имя, "чьё": "харнес", "критичность": "высокая",
              "свежая": dict(источник=источник, **свежая), "безопасность": источник,
              "задевает": [], "риск": "проба",
              "установка": {"дверь": "нет", "причина": "проба"}}
    if текущая is not None:
        данные["текущая"] = текущая
    return данные


def node(команда=("echo", "v22.20.0")):
    return часть("node", "nodejs-dist", {"команда": list(команда), "образец": ОБРАЗЕЦ_NODE}, ветка="22")


def claude(версия="2.1.299"):
    return часть("claude-code", "npm", {"команда": ["echo", f"{версия} (Claude Code)"],
                                         "образец": "^([0-9]+(\\.[0-9]+){1,3})"},
                 пакет="@anthropic-ai/claude-code")


def пакет_apt(имя, текущая, **доп):
    данные = часть(имя, "apt", {"команда": ["echo", текущая]}, пакет=имя)
    данные["критичность"] = "обычная"
    data = dict(данные)
    data.update(доп)
    return data


class Стенд:
    def __init__(self):
        self.корень = Path(tempfile.mkdtemp(prefix="стек-стенд-"))
        self.state, self.log, self.hb = (self.корень / n for n in ("state", "log", "hb"))
        self.src, self.bin, self.boot = (self.корень / n for n in ("src", "bin", "boot"))
        for каталог in (self.state, self.log, self.hb, self.src, self.bin, self.boot):
            каталог.mkdir()
        for файл in ФИКСТУРЫ.glob("*"):
            if файл.is_file():
                shutil.copy(файл, self.src / файл.name)
        self.реестр = self.корень / "stack.yaml"
        self.reboot_file = self.корень / "reboot-required"
        self.history = self.корень / "history.log"
        shutil.copy(ФИКСТУРЫ / "apt-history-tail.txt", self.history)
        (self.корень / "install.conf").write_text(
            f'LOG_DIR="{self.log}"\nSTATE_DIR="{self.state}"\nHEARTBEAT_DIR="{self.hb}"\n'
            f'PROJECT_DIR="{self.корень}"\n', encoding="utf-8")
        (self.корень / "harness.conf").write_text("", encoding="utf-8")
        self.вызовы = self.корень / "вызовы.log"
        # Штамп свежести списков apt (update-success-stamp): дверь стенда, чтобы замер не судил
        # штамп боевой машины. По умолчанию свежий — час до HARNESS_STACK_NOW прогона.
        self.apt_stamp = self.корень / "update-success-stamp"
        self.apt_stamp.write_text("", encoding="utf-8")

    def закрыть(self):
        shutil.rmtree(self.корень, ignore_errors=True)

    def записать_реестр(self, части, **верх):
        тело = {"части": части, "в_общей_строке_ОС": [], "не_судим": {}}
        тело.update(верх)
        self.реестр.write_text(yaml.safe_dump(тело, allow_unicode=True), encoding="utf-8")

    def заглушка(self, имя, код=0, вывод="", ошибка="", передать=None):
        """Команда в PATH стенда: пишет argv в журнал вызовов; `передать` — настоящая команда."""
        строки = ["#!/bin/sh", f'printf "{имя} %s\\n" "$*" >> "{self.вызовы}"']
        if передать:
            строки.append(f'exec {передать} "$@"')
        else:
            if вывод:
                строки.append(f"printf '%s\\n' '{вывод}'")
            if ошибка:
                строки.append(f"printf '%s\\n' '{ошибка}' >&2")
            строки.append(f"exit {код}")
        путь = self.bin / имя
        путь.write_text("\n".join(строки) + "\n", encoding="utf-8")
        путь.chmod(0o755)

    def окружение(self, **доп):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.корень),
               "LC_ALL": "C.UTF-8", "TZ": "Asia/Qostanay", "PYTHONDONTWRITEBYTECODE": "1",
               "HARNESS_STACK_REGISTRY": str(self.реестр), "HARNESS_STACK_SOURCES": str(self.src),
               "HARNESS_STACK_NOW": СЕЙЧАС, "HARNESS_STACK_BOOT_DIR": str(self.boot),
               "HARNESS_STACK_REBOOT_FILE": str(self.reboot_file),
               "HARNESS_APT_HISTORY": str(self.history),
               "HARNESS_INSTALL_CONF": str(self.корень / "install.conf"),
               "HARNESS_CONF": str(self.корень / "harness.conf"),
               "LOG_DIR": str(self.log), "STATE_DIR": str(self.state),
               "HEARTBEAT_DIR": str(self.hb), "PROJECT_DIR": str(self.корень),
               "HARNESS_STACK_APT_LISTS_STAMP": str(self.apt_stamp)}
        env.update(доп)
        fresh = (dt.datetime.fromisoformat(env["HARNESS_STACK_NOW"]) - dt.timedelta(hours=1)).timestamp()
        os.utime(self.apt_stamp, (fresh, fresh))
        return env

    def запустить(self, *args, **доп):
        готово = subprocess.run([sys.executable, "-B", str(ИНСТРУМЕНТ), *args], cwd=self.корень,
                                env=self.окружение(**доп), capture_output=True, text=True, timeout=120)
        return готово.returncode, готово.stdout, готово.stderr

    def замер(self):
        return json.loads((self.log / "stack" / "замер.json").read_text(encoding="utf-8"))

    def ряд(self, имя):
        return next(r for r in self.замер()["части"] if r["имя"] == имя)

    def журнал_вызовов(self):
        return self.вызовы.read_text(encoding="utf-8") if self.вызовы.exists() else ""

    def контекст(self, sf, **доп):
        параметры = dict(run=sf.run_argv, now=dt.datetime.fromisoformat(СЕЙЧАС), sources=self.src,
                         state_dir=self.state, work_dir=self.log / "stack", boot_dir=self.boot, reboot_file=self.reboot_file,
                         apt_history=self.history, env_path=f"{self.bin}:/usr/bin:/bin")
        параметры.update(доп)
        return sf.Ctx(**параметры)


# ── случаи ──────────────────────────────────────────────────────────────────
СЛУЧАИ = []


def случай(имя):
    def обёртка(функция):
        СЛУЧАИ.append((имя, функция))
        return функция
    return обёртка


def проверить(условие, сообщение):
    if not условие:
        raise AssertionError(сообщение)


def индекс_node():
    return json.loads((ФИКСТУРЫ / "nodejs-dist-index.json").read_text(encoding="utf-8"))


def кортеж(версия):
    return tuple(int(x) for x in версия.lstrip("v").split("."))


@случай("Б1 node отстала с заплаткой: 11 выпусков, 5 безопасности")
def б1():
    с = Стенд()
    try:
        с.записать_реестр([node()])
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 0, f"замер вернул {код}: {вывод}")
        строка = next((s for s in вывод.splitlines() if s.startswith("node ")), "")
        проверить("node отстала_с_заплаткой 22.20.0 → 22.23.4 выпусков_новее=11 безопасности=5 суток=" in строка,
                  f"строка node: {строка!r}")
        проверить("судимых частей 1" in вывод, "нет итога «судимых частей 1»")
        проверить(с.ряд("node")["состояние"] == "отстала_с_заплаткой", "замер.json без состояния")
    finally:
        с.закрыть()


@случай("Б1-cc Claude Code: 11 выпусков, безопасности 0, суток от записанного time")
def б1_cc():
    с = Стенд()
    try:
        с.записать_реестр([claude()])
        записанное = json.loads((с.src / "npm-claude-code-time.json").read_text(encoding="utf-8"))
        записанное["2.1.305-beta.1"] = "2026-10-20T10:00:00.000Z"    # предварительный — не выпуск
        записанное["2.1.311"] = "2026-10-24T10:00:00.000Z"           # новее npm view version — не выпуск
        (с.src / "npm-claude-code-time.json").write_text(json.dumps(записанное), encoding="utf-8")
        код, вывод, _ = с.запустить("--замер", "--печать")
        время = json.loads((ФИКСТУРЫ / "npm-claude-code-time.json").read_text(encoding="utf-8"))
        новее = {v: t for v, t in время.items() if v[:1].isdigit() and кортеж(v) > (2, 1, 299)}
        самая_старая = min(dt.datetime.fromisoformat(t.replace("Z", "+00:00")) for t in новее.values())
        суток = (dt.datetime.fromisoformat(СЕЙЧАС) - самая_старая).days
        проверить(len(новее) == 11, f"оракул: новее {len(новее)}")
        строка = next((s for s in вывод.splitlines() if s.startswith("claude-code ")), "")
        ждём = f"claude-code отстала 2.1.299 → 2.1.310 выпусков_новее=11 безопасности=0 суток={суток}"
        проверить(ждём in строка, f"строка: {строка!r}, ждали {ждём!r}")
    finally:
        с.закрыть()


@случай("Б2 свежая; 22.9.0 против 22.10.0 не сравнивается строкой")
def б2():
    с = Стенд()
    try:
        с.записать_реестр([node(("echo", "v22.23.4"))])
        _, вывод, _ = с.запустить("--замер", "--печать")
        проверить("node свежая 22.23.4" in вывод, f"свежая не названа: {вывод!r}")
        с.записать_реестр([node(("echo", "v22.9.0"))])
        (с.log / "stack" / "замер.json").unlink()     # смена версии между замерами — другой случай (Б15)
        код, вывод, _ = с.запустить("--замер", "--печать")
        ряд = с.ряд("node")
        ждём = len([e for e in индекс_node() if e["version"].startswith("v22.") and кортеж(e["version"]) > (22, 9, 0)])
        проверить(ряд["состояние"].startswith("отстала"), f"22.9.0 принята за {ряд['состояние']}")
        проверить(ряд["выпусков_новее"] == ждём, f"новее {ряд['выпусков_новее']}, оракул {ждём}")
    finally:
        с.закрыть()


@случай("Б3 источники недоступны: не узнать свежую, не «свежая», код 0")
def б3():
    с = Стенд()
    try:
        for файл in с.src.glob("*"):
            файл.unlink()
        с.записать_реестр([node()])
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 0, f"код {код}")
        проверить("node не_узнать_свежую" in вывод, f"вывод: {вывод!r}")
        проверить("свежая 22" not in вывод, "недоступный источник назван свежим")
    finally:
        с.закрыть()


@случай("Б4 часть не мерится: код 2 и причина")
def б4():
    с = Стенд()
    try:
        с.записать_реестр([node(("false",))])
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 2, f"код {код}, ждали 2")
        проверить("node не_узнать_текущую" in вывод, f"вывод: {вывод!r}")
        # Без образца форму текущей судит только valid_version: мусор — не версия.
        с.записать_реестр([пакет_apt("git", "мусор; x")])
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 2 and "git не_узнать_текущую" in вывод, f"текущая не той формы: код {код}, {вывод!r}")
    finally:
        с.закрыть()


@случай("Б4в есть_если файл и ключ: нет — не стоит; есть — мерится")
def б4в():
    с = Стенд()
    try:
        файл = пакет_apt("git", "1:2.43.0-1ubuntu7.3", **{"есть_если": {"файл": "$STATE_DIR/голос/python"}})
        ключ = dict(пакет_apt("git", "1:2.43.0-1ubuntu7.3", **{"есть_если": {"ключ": "VOICE=1"}}), имя="git-ключ")
        с.записать_реестр([файл, ключ])
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 0 and "git не_стоит" in вывод and "git-ключ не_стоит" in вывод, f"нет файла и ключа: {вывод!r}")
        (с.state / "голос").mkdir()
        (с.state / "голос" / "python").write_text("", encoding="utf-8")
        (с.корень / "harness.conf").write_text('VOICE="1"\n', encoding="utf-8")
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить("git отстала" in вывод and "git-ключ отстала" in вывод, f"файл и ключ есть, а: {вывод!r}")
    finally:
        с.закрыть()


@случай("Б4б часть, которой нет у клиента: не стоит, код 0; ошибка dpkg-query — код 2")
def б4б():
    с = Стенд()
    try:
        нет_стенда = пакет_apt("nginx-стенд", "1.0", **{"есть_если": {"пакет": "nginx-стенд"}})
        с.записать_реестр([нет_стенда])
        с.заглушка("dpkg-query", 1, ошибка="dpkg-query: no packages found matching nginx-стенд")
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 0 and "nginx-стенд не_стоит" in вывод, f"код {код}, вывод {вывод!r}")
        с.заглушка("dpkg-query", 2, ошибка="dpkg-query: error: cannot access archive")
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 2, f"поломка dpkg-query принята за «не стоит»: код {код}")
    finally:
        с.закрыть()


@случай("Б5 пустой реестр и «все чужие»: код 2, «судить нечего: 0»")
def б5():
    с = Стенд()
    try:
        с.записать_реестр([])
        код, вывод, ошибка = с.запустить("--замер", "--печать")
        проверить(код == 2 and "судить нечего: 0" in вывод + ошибка, f"пустой: код {код}, {вывод!r}")
        чужая = node()
        чужая["чьё"] = "чужое"
        с.записать_реестр([чужая])
        код, вывод, ошибка = с.запустить("--замер", "--печать")
        проверить(код == 2 and "судить нечего: 0" in вывод + ошибка, f"чужие: код {код}, {вывод!r}")
    finally:
        с.закрыть()


@случай("Б11 ответ источника не той формы: не узнать свежую, команда не исполнена")
def б11():
    for дурная in ("v22.23.4; touch X", "v22.23.4\n", "v$(id)", "--force"):
        с = Стенд()
        try:
            (с.src / "nodejs-dist-index.json").write_text(json.dumps(
                [{"version": дурная, "date": "2026-10-20", "security": False},
                 {"version": "v22.20.0", "date": "2025-10-16", "security": False}]), encoding="utf-8")
            с.записать_реестр([node()])
            код, вывод, _ = с.запустить("--замер", "--печать")
            проверить(код == 0, f"{дурная!r}: код {код}")
            проверить("node не_узнать_свежую" in вывод, f"{дурная!r} принята: {вывод!r}")
            проверить(not (с.корень / "X").exists(), f"{дурная!r}: файл X создан")
            журнал = (с.log / "stack-watch.log").read_text(encoding="utf-8")
            проверить("не той формы" in журнал, f"{дурная!r}: причина не в журнале")
        finally:
            с.закрыть()
    с = Стенд()
    try:
        (с.src / "npm-claude-code-version.txt").write_text("2.1.310; touch X\n", encoding="utf-8")
        с.записать_реестр([claude()])
        _, вывод, _ = с.запустить("--замер", "--печать")
        проверить("claude-code не_узнать_свежую" in вывод and not (с.корень / "X").exists(), f"npm: {вывод!r}")
    finally:
        с.закрыть()
    с = Стенд()
    try:
        (с.src / "nodejs-dist-index.json").write_text(json.dumps(
            [{"version": "v22.23.4" + "Щ" * 100, "date": "2026-10-20", "security": False}]), encoding="utf-8")
        с.записать_реестр([node()])
        с.запустить("--замер")
        журнал = (с.log / "stack-watch.log").read_text(encoding="utf-8")
        проверить("не той формы" in журнал and "Щ" * 41 not in журнал, "ответ источника в журнале длиннее 40 знаков")
    finally:
        с.закрыть()


@случай("Б11-apt версии apt разбираются, сравнивает dpkg --compare-versions (argv-списком)")
def б11_apt():
    с = Стенд()
    try:
        с.заглушка("dpkg", передать="/usr/bin/dpkg")
        с.записать_реестр([пакет_apt("runc", "1.3.3-0ubuntu1~24.04.3"),
                           пакет_apt("git", "1:2.43.0-1ubuntu7.3")])
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 0, f"код {код}: {вывод!r}")
        проверить(с.ряд("runc")["состояние"] == "отстала", f"runc: {с.ряд('runc')['состояние']}")
        проверить(с.ряд("git")["состояние"] == "отстала", f"git с эпохой: {с.ряд('git')['состояние']}")
        проверить("dpkg --compare-versions" in с.журнал_вызовов(), "сравнение шло не через dpkg")
        с.записать_реестр([пакет_apt("git", "1:2.43.0-1ubuntu7.4")])
        (с.log / "stack" / "замер.json").unlink()
        с.запустить("--замер")
        проверить(с.ряд("git")["состояние"] == "свежая", "равные версии не свежая")
    finally:
        с.закрыть()


@случай("Б11-security кандидат из -security виден; нет — безопасности 0")
def б11_security():
    с = Стенд()
    try:
        текст = (с.src / "apt-cache-policy.txt").read_text(encoding="utf-8")
        блок = текст.split("runc:")[1].split("nginx:")[0]
        правка = блок.replace("noble-updates", "noble-security", 1)
        проверить(правка != блок, "оракул: замена кармана не сработала")
        (с.src / "apt-cache-policy.txt").write_text(текст.replace(блок, правка), encoding="utf-8")
        с.записать_реестр([пакет_apt("runc", "1.3.3-0ubuntu1~24.04.3")])
        с.запустить("--замер")
        проверить(с.ряд("runc")["безопасности"] == 1, f"безопасности {с.ряд('runc')['безопасности']}")
        проверить(с.ряд("runc")["состояние"] == "отстала", f"обычная критичность названа {с.ряд('runc')['состояние']}")
        (с.src / "apt-cache-policy.txt").write_text(текст, encoding="utf-8")
        с.запустить("--замер")
        проверить(с.ряд("runc")["безопасности"] == 0, "карман updates принят за security")
    finally:
        с.закрыть()


@случай("Б11-phased поэтапный кандидат — не пункт, помечен «поэтапно»")
def б11_phased():
    с = Стенд()
    try:
        с.записать_реестр([пакет_apt("tmux", "3.4-1ubuntu0.1")])
        _, вывод, _ = с.запустить("--замер", "--печать")
        проверить("tmux поэтапно" in вывод, f"вывод: {вывод!r}")
    finally:
        с.закрыть()


@случай("Б12 отказ источника (403/404/429/таймаут): не узнать свежую, не текущую, код 0")
def б12():
    sf = загрузить_инструмент()
    с = Стенд()
    настоящий = urllib.request.urlopen
    try:
        с.записать_реестр([node()])
        части = sf.load_registry(с.реестр).parts
        for ошибка in (urllib.error.HTTPError("u", 403, "x", {}, None), urllib.error.HTTPError("u", 404, "x", {}, None),
                       urllib.error.HTTPError("u", 429, "x", {}, None), socket.timeout("t")):
            вызовы = []

            def подмена(*a, _e=ошибка, **k):
                вызовы.append(a)
                raise _e
            urllib.request.urlopen = подмена
            ряды = sf.measure(части, с.контекст(sf, sources=None))
            проверить(ряды[0]["состояние"] == "не_узнать_свежую", f"{ошибка!r}: {ряды[0]['состояние']}")
            # Причину судит СВОЯ ловушка http_get: общая ловушка разбора тоже даёт «не узнать», но словом «не разобран».
            своя = f"источник ответил {ошибка.code}" if isinstance(ошибка, urllib.error.HTTPError) else "источник недоступен"
            проверить(своя in ряды[0].get("причина", ""), f"{ошибка!r}: причина {ряды[0].get('причина')!r}, ждали «{своя}»")
            проверить(sf.exit_code(ряды) == 0, f"{ошибка!r}: код {sf.exit_code(ряды)}")
            проверить(len(вызовы) >= 1, "источник не опрошен — проба ничего не судила")
    finally:
        urllib.request.urlopen = настоящий
        с.закрыть()


@случай("Б-сеть записанные ответы: ноль обращений в сеть")
def б_сеть():
    sf = загрузить_инструмент()
    с = Стенд()
    настоящий = urllib.request.urlopen
    вызовы = []
    try:
        def подмена(*a, **k):
            вызовы.append(a)
            raise AssertionError("обращение в сеть мимо стенда")
        urllib.request.urlopen = подмена
        с.записать_реестр([node(), claude()])
        sf.measure(sf.load_registry(с.реестр).parts, с.контекст(sf))
        проверить(not вызовы, f"обращений в сеть: {len(вызовы)}")
    finally:
        urllib.request.urlopen = настоящий
        с.закрыть()


class _Ответ:
    def __init__(self, тело, etag="abc"):
        self.тело, self.headers, self.чтения = тело, {"ETag": etag}, []

    def read(self, n=-1):
        self.чтения.append(n)
        return self.тело if n < 0 else self.тело[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@случай("Б-кэш ETag уходит в If-None-Match, 304 берёт кэш; ответ больше 2 МБ — не узнать")
def б_кэш():
    sf = загрузить_инструмент()
    с = Стенд()
    настоящий = urllib.request.urlopen
    запросы = []
    try:
        с.записать_реестр([node()])
        части = sf.load_registry(с.реестр).parts
        тело = (ФИКСТУРЫ / "nodejs-dist-index.json").read_bytes()

        def первый(запрос, *a, **k):
            запросы.append(dict(запрос.header_items()))
            return _Ответ(тело)
        urllib.request.urlopen = первый
        ряды = sf.measure(части, с.контекст(sf, sources=None))
        проверить(ряды[0]["выпусков_новее"] == 11, f"первый замер: {ряды[0]}")

        def второй(запрос, *a, **k):
            запросы.append(dict(запрос.header_items()))
            raise urllib.error.HTTPError("u", 304, "Not Modified", {}, None)
        urllib.request.urlopen = второй
        ряды = sf.measure(части, с.контекст(sf, sources=None))
        проверить(any(k.lower() == "if-none-match" and v == "abc" for k, v in запросы[-1].items()),
                  f"заголовки второго запроса: {запросы[-1]}")
        проверить(ряды[0]["выпусков_новее"] == 11, f"304 не взял кэш: {ряды[0]}")

        огромный = _Ответ(тело + b" " * (3 * 1024 * 1024), etag="big")
        urllib.request.urlopen = lambda *a, **k: огромный
        shutil.rmtree(с.log / "stack" / "кэш")
        ряды = sf.measure(части, с.контекст(sf, sources=None))
        проверить(ряды[0]["состояние"] == "не_узнать_свежую", f"огромный ответ принят: {ряды[0]['состояние']}")
        проверить(огромный.чтения and all(0 <= n <= 2 * 1024 * 1024 + 1 for n in огромный.чтения),
                  f"чтение без предела: {огромный.чтения}")
    finally:
        urllib.request.urlopen = настоящий
        с.закрыть()


@случай("Б-ядро перезагрузка: новейшее ядро — по числам, не по строке; суток по mtime")
def б_ядро():
    с = Стенд()
    try:
        с.заглушка("uname", вывод="6.8.0-138-generic")
        for ядро in ("6.8.0-99", "6.8.0-138", "6.8.0-147"):
            (с.boot / f"vmlinuz-{ядро}-generic").write_text("", encoding="utf-8")
        с.reboot_file.write_text("", encoding="utf-8")
        момент = (dt.datetime.fromisoformat(СЕЙЧАС) - dt.timedelta(days=9, hours=2)).timestamp()
        os.utime(с.reboot_file, (момент, момент))
        перезагрузка = часть("перезагрузка", "перезагрузка", {"команда": ["uname", "-r"], "образец": ОБРАЗЕЦ_ЯДРА})
        перезагрузка["чьё"] = "машина"
        с.записать_реестр([перезагрузка])
        код, вывод, _ = с.запустить("--замер", "--печать")
        проверить(код == 0, f"код {код}: {вывод!r}")
        проверить("перезагрузка отстала 6.8.0-138 → 6.8.0-147" in вывод and "суток=9" in вывод, f"вывод: {вывод!r}")
        с.reboot_file.unlink()
        с.заглушка("uname", вывод="6.8.0-147-generic")
        _, вывод, _ = с.запустить("--замер", "--печать")
        проверить("перезагрузка свежая" in вывод, f"ядро новейшее, файла нет, а: {вывод!r}")
        с.reboot_file.write_text("", encoding="utf-8")
        _, вывод, _ = с.запустить("--замер", "--печать")
        проверить("перезагрузка отстала" in вывод, f"ядро новейшее, reboot-required есть, а: {вывод!r}")
    finally:
        с.закрыть()


@случай("Б1-pypi выпуски до info.version, без предварительных")
def б1_pypi():
    с = Стенд()
    try:
        путь = с.src / "pypi-faster-whisper.json"
        тело = json.loads(путь.read_text(encoding="utf-8"))
        проверить(тело["info"]["version"] == "1.2.1", f"оракул: info.version {тело['info']['version']}")
        тело["releases"]["1.2.0rc1"] = []      # предварительный — не выпуск
        тело["releases"]["1.3.0"] = []         # новее info.version — не выпуск
        путь.write_text(json.dumps(тело), encoding="utf-8")
        с.записать_реестр([часть("faster-whisper", "pypi", {"команда": ["echo", "1.1.0"]}, пакет="faster-whisper")])
        _, вывод, _ = с.запустить("--замер", "--печать")
        проверить("faster-whisper отстала_с_заплаткой" not in вывод
                  and "faster-whisper отстала 1.1.0 → 1.2.1 выпусков_новее=3 " in вывод, f"вывод: {вывод!r}")
    finally:
        с.закрыть()


@случай("Б-ОС общая строка: считаются только карманы -updates/-security, заплатки — по -security")
def б_ос():
    с = Стенд()
    try:
        путь = с.src / "apt-list-upgradable.txt"
        текст = путь.read_text(encoding="utf-8")
        правка = текст.replace("apparmor/noble-updates ", "apparmor/noble-updates,noble-security ", 1)
        проверить(правка != текст, "оракул: строки apparmor нет")
        путь.write_text(правка, encoding="utf-8")
        с.записать_реестр([{"имя": "пакеты-ubuntu", "чьё": "машина", "критичность": "обычная",
                            "свежая": {"источник": "apt-ждущие", "карман": "-(updates|security)$"}}])
        _, вывод, _ = с.запустить("--замер", "--печать")
        # 95 строк карманов noble-updates в записанном ответе (grep -cE '^[^/ ]+/\S*-(updates|security)'), чужих 38.
        проверить("пакеты-ubuntu отстала ? → - выпусков_новее=95 безопасности=1 " in вывод, f"вывод: {вывод!r}")
    finally:
        с.закрыть()


def faster_whisper():
    return часть("faster-whisper", "pypi", {"команда": ["echo", "1.1.0"]}, пакет="faster-whisper")


@случай("C1-форма чужой ответ не той формы (pypi списком, npm time списком): не узнать свежую, код 0, замер записан")
def c1_wrong_shape():
    for label, body, make_part in (("pypi-faster-whisper.json", "[1]", faster_whisper),
                                   ("npm-claude-code-time.json", "[]", claude)):
        stand = Стенд()
        try:
            (stand.src / label).write_text(body, encoding="utf-8")
            part = make_part()
            stand.записать_реестр([part, node()])
            code, out, err = stand.запустить("--замер", "--печать")
            проверить(code == 0, f"{label}={body}: код {code}, ждали 0; {err.strip().splitlines()[-1:]}")
            проверить(stand.ряд(part["имя"])["состояние"] == "не_узнать_свежую",
                      f"{label}: {stand.ряд(part['имя'])['состояние']}")
            проверить(stand.ряд("node")["состояние"] == "отстала_с_заплаткой", "соседняя часть не измерена")
            log = (stand.log / "stack-watch.log").read_text(encoding="utf-8")
            проверить(f"{part['имя']} не_узнать_свежую" in log, f"{label}: причины нет в журнале")
        finally:
            stand.закрыть()


@случай("C1-http обрыв HTTP не подкласса OSError (IncompleteRead, BadStatusLine, InvalidURL): не узнать свежую, не исключение")
def c1_http_exceptions():
    sf = загрузить_инструмент()
    stand = Стенд()
    real = urllib.request.urlopen
    try:
        stand.записать_реестр([node()])
        parts = sf.load_registry(stand.реестр).parts
        for error in (http.client.IncompleteRead(b"[{"), http.client.BadStatusLine("HTTP/9"),
                      http.client.InvalidURL("bad"), ValueError("unknown url type: bad")):
            # InvalidURL — подкласс HTTPException, не ValueError; ValueError бросает сам urllib (кривой адрес).
            def fake(*a, _e=error, **k):
                raise _e
            urllib.request.urlopen = fake
            try:
                rows = sf.measure(parts, stand.контекст(sf, sources=None))
            except Exception as exc:  # noqa: BLE001 — любое исключение здесь и есть дефект
                raise AssertionError(f"{type(error).__name__}: замер упал {exc!r}")
            проверить(rows[0]["состояние"] == "не_узнать_свежую", f"{type(error).__name__}: {rows[0]['состояние']}")
            # Общая ловушка read_latest тоже дала бы «не узнать» — судим слово СВОЕЙ ловушки http_get.
            проверить("источник недоступен" in rows[0].get("причина", ""),
                      f"{type(error).__name__}: причина {rows[0].get('причина')!r}, ждали «источник недоступен»")
    finally:
        urllib.request.urlopen = real
        stand.закрыть()


@случай("M6 битая дата одного источника: эта часть не узнать свежую, остальные измерены, код 0")
def m6_bad_date():
    stand = Стенд()
    try:
        times = json.loads((stand.src / "npm-claude-code-time.json").read_text(encoding="utf-8"))
        times["2.1.305"] = "2026-13-40T10:00:00.000Z"
        (stand.src / "npm-claude-code-time.json").write_text(json.dumps(times), encoding="utf-8")
        stand.записать_реестр([claude(), node()])
        code, out, _ = stand.запустить("--замер", "--печать")
        проверить(code == 0, f"npm: код {code}: {out!r}")
        проверить(stand.ряд("claude-code")["состояние"] == "не_узнать_свежую", f"npm: {stand.ряд('claude-code')}")
        проверить(stand.ряд("node")["состояние"] == "отстала_с_заплаткой", "npm: node не измерен")
    finally:
        stand.закрыть()
    stand = Стенд()
    try:
        index = json.loads((stand.src / "nodejs-dist-index.json").read_text(encoding="utf-8"))
        target = next(e for e in index if e["version"] == "v22.23.4")
        target["date"] = "2026-13-40"
        (stand.src / "nodejs-dist-index.json").write_text(json.dumps(index), encoding="utf-8")
        stand.записать_реестр([node(), claude()])
        code, out, _ = stand.запустить("--замер", "--печать")
        проверить(code == 0, f"nodejs: код {code}: {out!r}")
        проверить(stand.ряд("node")["состояние"] == "не_узнать_свежую", f"nodejs: {stand.ряд('node')}")
        проверить(stand.ряд("claude-code")["состояние"] == "отстала", "nodejs: claude-code не измерен")
    finally:
        stand.закрыть()


@случай("M5а списки apt старше суток: apt-части не узнать свежую «apt-списки старше суток», apt-get update не зовётся")
def m5_apt_lists_stale():
    stand = Стенд()
    try:
        stand.заглушка("apt-get")
        stand.заглушка("apt")
        stand.записать_реестр([пакет_apt("git", "1:2.43.0-1ubuntu7.3"), node()])
        stand.запустить("--замер")
        проверить(stand.ряд("git")["состояние"] == "отстала", f"контроль: свежий штамп, git {stand.ряд('git')['состояние']}")
        old = stand.корень / "old-stamp"
        old.write_text("", encoding="utf-8")
        moment = (dt.datetime.fromisoformat(СЕЙЧАС) - dt.timedelta(days=2)).timestamp()
        os.utime(old, (moment, moment))
        stand.запустить("--замер", HARNESS_STACK_APT_LISTS_STAMP=str(old))
        row = stand.ряд("git")
        проверить(row["состояние"] == "не_узнать_свежую" and "apt-списки старше суток" in row.get("причина", ""),
                  f"списки двухсуточные, а git {row['состояние']} {row.get('причина')!r}")
        проверить(stand.ряд("node")["состояние"] == "отстала_с_заплаткой", "не apt-часть задета штампом apt")
        проверить("apt-get" not in stand.журнал_вызовов() and "apt update" not in stand.журнал_вызовов(),
                  f"замер сам обновлял списки: {stand.журнал_вызовов()!r}")
    finally:
        stand.закрыть()


# ── гейт реестра ────────────────────────────────────────────────────────────
def стенд_установщика(с, лишняя_строка=""):
    каталог = с.корень / "установщик"
    (каталог / "harness" / "scripts").mkdir(parents=True, exist_ok=True)
    (каталог / "USTANOVIT.sh").write_text((ФИКСТУРЫ / "installer-excerpt.txt").read_text(encoding="utf-8")
                                          + лишняя_строка, encoding="utf-8")
    shutil.copy(ФИКСТУРЫ / "install-docker-excerpt.txt", каталог / "harness" / "scripts" / "install-docker.sh")
    return каталог


def гейт(с, реестр, установщик, окружение=None, **доп):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(с.корень), "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "HARNESS_STACK_REGISTRY": str(реестр)}
    if установщик is not None:
        env["HARNESS_STACK_INSTALLER"] = str(установщик)
    env.update(доп)
    готово = subprocess.run([sys.executable, "-B", str(ГЕЙТ)], env=окружение or env, cwd=с.корень,
                            capture_output=True, text=True, timeout=120)
    return готово.returncode, готово.stdout + готово.stderr


def правка_живого(с, изменить):
    данные = yaml.safe_load(живой_реестр().read_text(encoding="utf-8"))
    изменить(данные)
    путь = с.корень / "копия.yaml"
    путь.write_text(yaml.safe_dump(данные, allow_unicode=True), encoding="utf-8")
    return путь


@случай("Г1 живой реестр против живого установщика и против записанного: код 0, судимых частей ≥ 10")
def г1():
    с = Стенд()
    try:
        код, вывод = гейт(с, живой_реестр(), None, окружение=dict(os.environ, HARNESS_STACK_REGISTRY=str(живой_реестр())))
        проверить(код == 0, f"живой: код {код}: {вывод}")
        число = int(вывод.split("судимых частей ")[1].split(",")[0])
        проверить(число >= 10, f"судимых частей {число}")
        код, вывод = гейт(с, живой_реестр(), стенд_установщика(с))
        проверить(код == 0, f"записанный установщик: код {код}: {вывод}")
    finally:
        с.закрыть()


@случай("Г2 снята часть node: код 1, названо nodejs")
def г2():
    с = Стенд()
    try:
        реестр = правка_живого(с, lambda d: d.__setitem__("части", [p for p in d["части"] if p["имя"] != "node"]))
        код, вывод = гейт(с, реестр, стенд_установщика(с))
        проверить(код == 1 and "nodejs" in вывод, f"код {код}: {вывод}")
    finally:
        с.закрыть()


@случай("Г3 пакет установщика без решения: код 1, названо htop")
def г3():
    с = Стенд()
    try:
        установщик = стенд_установщика(с, '  делай "apt-get install -y htop"\n')
        код, вывод = гейт(с, живой_реестр(), установщик)
        проверить(код == 1 and "htop" in вывод, f"код {код}: {вывод}")
        установщик = стенд_установщика(с, '  делай "npx -y playwright install-deps firefox"\n')
        код, вывод = гейт(с, живой_реестр(), установщик)
        проверить(код == 1 and "install-deps firefox" in вывод, f"npx без причины: код {код}: {вывод}")
        реестр = правка_живого(с, lambda d: d["в_общей_строке_ОС"].append("мёртвый-пакет"))
        код, вывод = гейт(с, реестр, стенд_установщика(с))
        проверить(код == 1 and "мёртвый-пакет" in вывод, f"мёртвая запись общей строки: код {код}: {вывод}")
        # Не установка: комментарий, совет в echo, цель перенаправления после пакетов.
        установщик = стенд_установщика(с, '  # apt-get install -y htop\n  echo "поставьте: apt-get install -y htop"\n'
                                          '  apt-get install -y tmux 2>> install.log\n')
        код, вывод = гейт(с, живой_реестр(), установщик)
        проверить(код == 0, f"не-установка прочитана как установка: код {код}: {вывод}")
    finally:
        с.закрыть()


@случай("Г4 часть «чьё: харнес» без установщика и без «откуда: вручную»: код 1; с пометкой — 0")
def г4():
    с = Стенд()
    try:
        лишняя = часть("лишняя-часть", "apt", {"команда": ["echo", "1.0"]}, пакет="лишняя-часть")
        реестр = правка_живого(с, lambda d: d["части"].append(dict(лишняя)))
        код, вывод = гейт(с, реестр, стенд_установщика(с))
        проверить(код == 1 and "лишняя-часть" in вывод, f"код {код}: {вывод}")
        лишняя["откуда"] = "вручную"
        реестр = правка_живого(с, lambda d: d["части"].append(dict(лишняя)))
        код, вывод = гейт(с, реестр, стенд_установщика(с))
        проверить(код == 0, f"с пометкой код {код}: {вывод}")
    finally:
        с.закрыть()


@случай("Г5 argv строкой: код 1")
def г5():
    с = Стенд()

    def испортить(данные):
        for p in данные["части"]:
            if p["имя"] == "claude-code":
                p["установка"]["argv"] = "npm install -g x"
    def поломка(имя, правка):
        def изменить(данные):
            правка(next(p for p in данные["части"] if p["имя"] == имя))
        return изменить
    try:
        код, вывод = гейт(с, правка_живого(с, испортить), стенд_установщика(с))
        проверить(код == 1 and "argv" in вывод, f"код {код}: {вывод}")
        for слово, правка in (("образец", поломка("node", lambda p: p["текущая"].__setitem__("образец", "(("))),
                              ("источник", поломка("git", lambda p: p["свежая"].__setitem__("источник", "нет-такого"))),
                              ("«риск»", поломка("git", lambda p: p.pop("риск"))),
                              ("«чьё»", поломка("git", lambda p: p.__setitem__("чьё", "кто-то"))),
                              ("нет «текущая»", поломка("git", lambda p: p.pop("текущая")))):
            код, вывод = гейт(с, правка_живого(с, правка), стенд_установщика(с))
            проверить(код == 1 and слово in вывод, f"{слово}: код {код}: {вывод}")
    finally:
        с.закрыть()


@случай("Г6 установщика нет: код 0 и «сверка с установщиком невозможна»; пустой реестр — код 1")
def г6():
    с = Стенд()
    try:
        пусто = с.корень / "пусто"
        пусто.mkdir()
        код, вывод = гейт(с, живой_реестр(), пусто)
        проверить(код == 0 and "сверка с установщиком невозможна" in вывод, f"код {код}: {вывод}")
        реестр = правка_живого(с, lambda d: d.__setitem__("части", []))
        код, вывод = гейт(с, реестр, пусто)
        проверить(код == 1 and "судимых частей 0" in вывод, f"пустой реестр: код {код}: {вывод}")
    finally:
        с.закрыть()


@случай("M4 гейт судит словари данных: «есть_если» вне PRESENCE и «критичность» вне {высокая, обычная} — код 1")
def m4_gate_dictionaries():
    stand = Стенд()

    def broken(name, change):
        def edit(data):
            change(next(p for p in data["части"] if p["имя"] == name))
        return edit
    try:
        for word, change in (("есть_если", broken("git", lambda p: p.__setitem__("есть_если", {"пакеты": "git"}))),
                             ("критичность", broken("node", lambda p: p.__setitem__("критичность", "высокаЯ")))):
            code, out = гейт(stand, правка_живого(stand, change), стенд_установщика(stand))
            проверить(code == 1 and word in out, f"опечатка «{word}»: код {code}: {out}")
    finally:
        stand.закрыть()


MACHINE_FACT = re.compile(r"node_\d+\.x|nodesource|https?://deb\.|/etc/apt/sources")


@случай("M3 реестр пакета не несёт фактов этой машины (адрес и ветка источника apt)")
def m3_no_machine_facts():
    found = [line.strip() for line in живой_реестр().read_text(encoding="utf-8").split("\n") if MACHINE_FACT.search(line)]
    проверить(not found, f"в реестре пакета факт этой машины: {found}")


@случай("Г-хост гейт из env -i и с чужим HOME даёт тот же код")
def г_хост():
    с = Стенд()
    try:
        установщик = стенд_установщика(с)
        обычный, _ = гейт(с, живой_реестр(), установщик)
        чистый = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "HARNESS_STACK_REGISTRY": str(живой_реестр()),
                  "HARNESS_STACK_INSTALLER": str(установщик)}
        код, вывод = гейт(с, живой_реестр(), установщик, окружение=чистый)
        проверить(код == обычный == 0, f"обычный {обычный}, env -i {код}: {вывод}")
    finally:
        с.закрыть()


def main() -> int:
    неудач = 0
    for имя, функция in СЛУЧАИ:
        try:
            функция()
            print(f"  ок      {имя}")
        except Exception as ошибка:  # noqa: BLE001 — любая причина красного называется в строке случая
            неудач += 1
            конец = traceback.format_exc().strip().splitlines()[-1]
            print(f"  ПРОВАЛ  {имя}: {конец}")
            if os.environ.get("STACK_TEST_TRACE"):
                traceback.print_exc()
    print(f"СВЕЖЕСТЬ СТЕКА Ф1: случаев {len(СЛУЧАИ)}, неудач {неудач}")
    return 1 if неудач else 0


if __name__ == "__main__":
    sys.exit(main())
