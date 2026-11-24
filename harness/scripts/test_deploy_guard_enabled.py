#!/usr/bin/env python3
"""Проба выключателя выката DEPLOY_ENABLED в deploy_guard: выключил — выката нет.

Приказ владельца 17.11.2026 (память/owner-orders-17-11-verbatim.md): выключатель
в панели для выката; выключил — КОД не даёт делать. Выключатель — не вопрос
владельцу: DEPLOY_ENABLED="0" отказывает выкату ДО суда согласия, и ни полный
уровень автоматизации, ни отпущенная ручка DEPLOY_NEEDS_OWNER, ни согласие
владельца (заранее — DEPLOY_CONSENT_STANDING, или кнопкой «Да») его не снимают.
Пуш и публикация ключом не затрагиваются: у них свои ручки.

Ключ читается у НОСИТЕЛЯ (owner_word.конфиг_права), как AUTOMATION_LEVEL: проба
подменяет owner_word.носитель на носитель своего двора — свой harness.conf,
install.conf, каталог журналов, git-репозиторий дерева выката. Занятость машины
и две смены подменены пустыми: предмет пробы — выключатель, а не /proc.
"""
import contextlib
import importlib.util
import io
import json
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_YARD = Path(tempfile.mkdtemp(prefix="proba-dg-enabled-"))
_LOG = _YARD / "log"
_PROJECT = _YARD / "proj"
for _dir in (_LOG, _PROJECT):
    _dir.mkdir(parents=True)
_GIT = dict(os.environ, GIT_AUTHOR_NAME="проба", GIT_AUTHOR_EMAIL="p@stand.invalid",
            GIT_COMMITTER_NAME="проба", GIT_COMMITTER_EMAIL="p@stand.invalid",
            GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
(_PROJECT / "a.txt").write_text("a\n", encoding="utf-8")
for _args in (["init", "-q", "-b", "master"], ["add", "-A"], ["commit", "-q", "-m", "проба"]):
    subprocess.run(["git", "-C", str(_PROJECT), *_args], check=True, capture_output=True, env=_GIT)
_HARNESS_CONF = _YARD / "harness.conf"
_INSTALL_CONF = _YARD / "install.conf"
_OWNER = 777000


def configs(knobs=None, autonomy="semi"):
    """Конфиги двора; значение None у ключа — ключа в конфиге НЕТ."""
    keys = {"PUSH_NEEDS_OWNER": "1", "DEPLOY_NEEDS_OWNER": "1", "PUBLISH_NEEDS_OWNER": "1",
            "DEPLOY_CONSENT_STANDING": "0", "CONFIRM_MAX_AGE_MIN": "60", "CONSENT_BUTTON_TTL_MIN": "15",
            "SERVICE_LOCKS_SUBDIR": "services", "PUBLIC_REMOTE": "jig", **(knobs or {})}
    _HARNESS_CONF.write_text("".join(f'{k}="{v}"\n' for k, v in keys.items() if v is not None),
                             encoding="utf-8")
    _INSTALL_CONF.write_text(f'LOG_DIR="{_LOG}"\nAUTONOMY="{autonomy}"\nPROJECT_DIR="{_PROJECT}"\n'
                             f'TG_CHAT_ID="{_OWNER}"\n', encoding="utf-8")


configs()
os.environ.update(HARNESS_CONF=str(_HARNESS_CONF), HARNESS_LOG_DIR=str(_LOG), HARNESS_TASK="-")

# Мутационная проба подсовывает испорченную копию ворот этой дверью и требует красного.
_under_test = os.environ.get("DEPLOY_GUARD_UNDER_TEST")
if _under_test:
    _spec = importlib.util.spec_from_file_location("deploy_guard", _under_test)
    g = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(g)
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import deploy_guard as g
import owner_word  # noqa: E402


def stand_carrier():
    knobs = owner_word.конф.из_файла(str(_HARNESS_CONF))
    return owner_word.Носитель(папка=_LOG, uid=None, паспорт=owner_word.конф.из_файла(str(_INSTALL_CONF)),
                               граница=_YARD, окно_мин=int(knobs.get("CONFIRM_MAX_AGE_MIN") or 60),
                               срок_кнопки_мин=int(knobs.get("CONSENT_BUTTON_TTL_MIN") or 15), ручки=knobs)


owner_word.носитель = stand_carrier
g.LOG_DIR = _LOG
g.LOCKS = _LOG / "locks"
g.две_смены = lambda: None
g.лок_файлы = lambda: []
g.долгие_процессы = lambda: []
for _action in ("deploy", "push", "публикация"):
    g.ПРЕДМЕТЫ[_action] = lambda: "S"

paths = 0
failures = 0


def check(name, ok, details=""):
    global paths, failures
    paths += 1
    if ok:
        print(f"ок  · {name}")
    else:
        failures += 1
        print(f"ПЛОХО · {name} — {details[:240]}")


def stamp(minutes_ago):
    return (datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds")


def clean_journals():
    for name in ("inbox.jsonl", "consent-buttons.jsonl", "consent-requests.jsonl", "confirmations.jsonl"):
        (_LOG / name).write_text("", encoding="utf-8")


def button_yes(kind):
    """«Да» владельца кнопкой на предмет S — живой порядок: просьба, затем нажатие."""
    nonce = f"n{time.time_ns()}"
    with open(_LOG / "consent-requests.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"v": 1, "ts": stamp(1.5), "src": "scripts/ask-consent.py",
                                 "event": "consent.request", "outcome": "ok",
                                 "data": {"nonce": nonce, "вид": kind, "цель": "S", "почему": "проба"}},
                                ensure_ascii=False) + "\n")
    with open(_LOG / "consent-buttons.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"v": 1, "ts": stamp(1), "src": "проба", "event": "consent.yes", "outcome": "ok",
                                 "data": {"nonce": nonce, "вид": kind, "цель": "S", "message_id": 4000,
                                          "from_id": _OWNER}}, ensure_ascii=False) + "\n")


def call(action, *keys):
    """main() ворот в этом процессе: (код, вывод)."""
    saved, sys.argv = sys.argv, ["deploy_guard.py", "--действие", action, *keys]
    g._ПРЕДМЕТ.clear()
    g.ПУШ = g.ВЫКАТ = g.ПУБЛИКАЦИЯ = None
    g.МОМЕНТ_СВЕЖЕСТИ = None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = g.main()
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
            except Exception as err:            # noqa: BLE001
                code = f"исключение {type(err).__name__}: {err}"
    finally:
        sys.argv = saved
    return code, out.getvalue()


OFF = {"DEPLOY_ENABLED": "0"}
# (ручки, автономия, кнопка «Да» на выкат, что проверяем)
DEPLOY_OFF = [
    ({**OFF, "AUTOMATION_LEVEL": "full"}, "semi", False, "полный уровень автоматизации"),
    ({**OFF, "DEPLOY_NEEDS_OWNER": "0"}, "semi", False, "ручка «спросить меня» отпущена"),
    ({**OFF, "DEPLOY_CONSENT_STANDING": "1"}, "semi", False, "согласие владельца заранее"),
    (OFF, "full", False, "AUTONOMY=full"),
    (OFF, "semi", True, "«Да» кнопкой на этот выкат"),
    ({**OFF, "AUTOMATION_LEVEL": "full", "DEPLOY_NEEDS_OWNER": "0", "DEPLOY_CONSENT_STANDING": "1"},
     "full", True, "всё открыто разом"),
]
for knobs, autonomy, button, what in DEPLOY_OFF:
    configs(knobs, autonomy)
    for keys, how in ((("--только-согласие",), "ворота согласия"), ((), "полный зов выката")):
        clean_journals()
        if button:
            button_yes("выкат")
        code, output = call("deploy", *keys)
        check(f"БОЛЬНОЙ СЛУЧАЙ: DEPLOY_ENABLED=0 + {what} · {how} → выкат закрыт, отказ называет ключ",
              code == 1 and "DEPLOY_ENABLED" in output, f"код {code!r} · {output.strip()[-200:]!r}")

# Выключатель — не вопрос: отказ не зовёт просить кнопку и не печатает «согласие кнопкой».
configs(OFF)
clean_journals()
button_yes("выкат")
code, output = call("deploy", "--только-согласие")
check("DEPLOY_ENABLED=0: отказ не предлагает просить «Да» и не судит кнопку",
      "ask-consent" not in output and "согласие кнопкой" not in output, output.strip()[-200:])

# Пуш и публикация ключом не затрагиваются.
for action in ("push", "публикация"):
    knob = {"push": "PUSH_NEEDS_OWNER", "публикация": "PUBLISH_NEEDS_OWNER"}[action]
    configs({**OFF, knob: "0"})
    clean_journals()
    code, output = call(action, "--только-согласие")
    check(f"DEPLOY_ENABLED=0 · {action} с отпущенной ручкой → открыт (ключ его не касается)",
          code == 0, f"код {code!r} · {output.strip()[-200:]!r}")

# Включён или ключа нет (умолчание «1») — выкат судится, как прежде.
for value, what in (("1", "DEPLOY_ENABLED=1"), (None, "ключа нет")):
    configs({"DEPLOY_ENABLED": value, "AUTOMATION_LEVEL": "full"})
    clean_journals()
    code, output = call("deploy")
    check(f"{what} + полный уровень → выкат открыт", code == 0, f"код {code!r} · {output.strip()[-200:]!r}")
    configs({"DEPLOY_ENABLED": value})
    clean_journals()
    code, output = call("deploy", "--только-согласие")
    check(f"{what} + ручка «спросить меня», кнопки нет → закрыт ради кнопки, не выключателем",
          code == 1 and "кнопк" in output.lower() and "DEPLOY_ENABLED" not in output,
          f"код {code!r} · {output.strip()[-200:]!r}")

# CR-5 (ревью кода 20.11): выкат открыт только при "1" (или ключа нет — умолчание старой
# установки); любое иное значение — ручная опечатка выключателя — выкат закрывает, как у
# соседа BACKGROUND_PROCESSES (открыт только при «on»). Полный уровень: иначе закрыл бы суд
# согласия, а не выключатель. Поломка, что оставит зелёным: закрыт при «0» и «off», но
# сравнение без снятия пробелов («0 »); закрыт при «0»/«no»/«off», но «false» и прочее открыто.
for value in ("off", "no", "0 ", "false"):
    configs({"DEPLOY_ENABLED": value, "AUTOMATION_LEVEL": "full"})
    for keys, how in ((("--только-согласие",), "ворота согласия"), ((), "полный зов выката")):
        clean_journals()
        code, output = call("deploy", *keys)
        check(f"CR-5: БОЛЬНОЙ СЛУЧАЙ: DEPLOY_ENABLED={value!r} + полный уровень · {how} → закрыт, "
              f"отказ называет ключ", code == 1 and "DEPLOY_ENABLED" in output,
              f"код {code!r} · {output.strip()[-200:]!r}")

# SR2-1 (ревью спеки 20.11, итерация 2): слова отказа для владельца — по-русски, имя
# действия «выкат», а не «deploy»; ключ и значение названы. Те же слова — в спеке
# §4.1 п.2, «Для человека» и живой проверке §7 п.3.
# Поломка, что оставит зелёным: «выкат выключен» без значения ключа — ловит суд
# значения «off»; слова только на одном пути main — ловят оба пути.
for value in ("0", "off"):
    configs({"DEPLOY_ENABLED": value, "AUTOMATION_LEVEL": "full"})
    for keys, how in ((("--только-согласие",), "ворота согласия"), ((), "полный зов выката")):
        clean_journals()
        code, output = call("deploy", *keys)
        words = f'выкат выключен: DEPLOY_ENABLED="{value}" в harness.conf'
        check(f"SR2-1: DEPLOY_ENABLED={value!r} · {how} → отказ словами «{words}»",
              code == 1 and words in output, f"код {code!r} · {output.strip()[-200:]!r}")

# Окружение не носитель: DEPLOY_ENABLED=0 в среде при «1» у носителя выкат не закрывает и наоборот.
configs({"DEPLOY_ENABLED": "1", "AUTOMATION_LEVEL": "full"})
os.environ["DEPLOY_ENABLED"] = "0"
code_env_off, _ = call("deploy")
configs({**OFF, "AUTOMATION_LEVEL": "full"})
os.environ["DEPLOY_ENABLED"] = "1"
code_env_on, output = call("deploy")
del os.environ["DEPLOY_ENABLED"]
check("ключ судится у носителя: окружение его не подменяет ни в одну сторону",
      code_env_off == 0 and code_env_on == 1, f"среда 0 → {code_env_off!r}, среда 1 → {code_env_on!r}")

shutil.rmtree(_YARD, ignore_errors=True)
print(f"ВЫКЛЮЧАТЕЛЬ-ВЫКАТА: путей {paths}, неудач {failures}")
sys.exit(1 if failures else 0)
