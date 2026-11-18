#!/usr/bin/env python3
"""Проба строки «аккаунт: …» — на каком аккаунте смена, её помощники и демоны.

Владелец 18.11.2026: видно это было только в терминале сторожа, а надо — в
статусе канала и на старте смены, и в каждой копии харнеса. Читатель один —
account-switch.py --сейчас; status-report.py и хук session_state.sh только
печатают его строку.

Что ловится: строки нет в статусе или на старте смены; смена и демоны на
разных аккаунтах, а строка этого не говорит; «/login» внутри сессии выдан за
аккаунт из state.json; поток демонов шёл не на профиле запуска, а строка
молчит; в выводе ключ; без профилей (копия на /login) — лишний шум.

Стенд свой: временный каталог, копии profiles.json/state.json рождены
прогоном (HARNESS_ACCOUNT_DIR), фиктивные ключи (SECRETS_DIR), свой LOG_DIR с
потоком демонов, свой harness.conf с несуществующей сессией tmux. Боевые
аккаунты и смена не трогаются.

Двери мутаций: ACCOUNT_SWITCH_UNDER_TEST, HARNESS_STATUS_REPORT_UNDER_TEST,
SESSION_STATE_SH — пути к копиям.

    python3 scripts/test_accounts_now.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGIN = HERE / "check-login-expiry.py"
SWITCH = Path(os.environ.get("ACCOUNT_SWITCH_UNDER_TEST") or HERE / "account-switch.py")
REPORT = Path(os.environ.get("HARNESS_STATUS_REPORT_UNDER_TEST") or HERE / "status-report.py")
_крюк = HERE / "hooks" / "session_state.sh"
HOOK = Path(os.environ.get("SESSION_STATE_SH") or (_крюк if _крюк.is_file() else HERE.parent / "hooks" / "session_state.sh"))
КЛЮЧИ = {"alfa": "proba-klyuch-alfa", "beta": "proba-klyuch-beta", "gamma": "proba-klyuch-gamma"}


def отпечаток(ключ):
    return hashlib.sha256(ключ.encode()).hexdigest()[:8]


def load(имя, путь):
    spec = importlib.util.spec_from_file_location(имя, путь)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def стенд(корень: Path, идёт="alfa", запуск="alfa", профили=True):
    acc, sec, log = корень / "acc", корень / "secrets", корень / "log"
    for d in (acc, sec, log / "demon-stream", корень / "bin"):
        d.mkdir(parents=True, exist_ok=True)
    for имя, ключ in КЛЮЧИ.items():
        (sec / f"key-{имя}").write_text(ключ, encoding="utf-8")
    if профили:
        (acc / "profiles.json").write_text(json.dumps({"профили": [
            {"имя": имя, "подпись": f"подпись-{имя}", "орг": f"org-{имя}", "ключ": f"key-{имя}",
             "отпечаток": отпечаток(ключ)} for имя, ключ in КЛЮЧИ.items()]}, ensure_ascii=False), encoding="utf-8")
        (acc / "state.json").write_text(json.dumps({"запуск": запуск, "идёт": идёт}, ensure_ascii=False),
                                        encoding="utf-8")
    (корень / "harness.conf").write_text('TMUX_SESSION="net-takoj-sessii-proba"\n', encoding="utf-8")
    (корень / "install.conf").write_text("", encoding="utf-8")
    # Боевой сервер tmux не спрашивается: панели у стенда нет.
    (корень / "bin" / "tmux").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (корень / "bin" / "tmux").chmod(0o755)
    return {**{к: з for к, з in os.environ.items() if к not in ("CLAUDE_CODE_OAUTH_TOKEN", "TMUX", "TMUX_PANE")},
            "HARNESS_ACCOUNT_DIR": str(acc), "SECRETS_DIR": str(sec), "LOG_DIR": str(log),
            "HARNESS_CONF": str(корень / "harness.conf"), "HARNESS_HOME": str(корень),
            "HARNESS_INSTALL_CONF": str(корень / "install.conf"),
            "PATH": f"{корень / 'bin'}:{os.environ.get('PATH', '')}"}


def поток(корень: Path, имя: str, чей: str, возраст_с: int):
    путь = корень / "log" / "demon-stream" / имя
    путь.write_text(json.dumps({"type": "harness_account", "отпечаток": чей}) + "\n"
                    + '{"type":"result"}\n', encoding="utf-8")
    t = time.time() - возраст_с
    os.utime(путь, (t, t))


class Сторож:
    """Подставной сторож входа: процесс смены и его ключ — из стенда, не из /proc."""

    def __init__(self, настоящий, pid=None, ключ=None, вход_в_сессии=False):
        self._н, self._pid, self._ключ, self._вход = настоящий, pid, ключ, вход_в_сессии

    def __getattr__(self, имя):
        return getattr(self._н, имя)

    def процесс_смены(self, *a):
        return self._pid

    def ключ_процесса(self, pid):
        return self._ключ

    def вход_после_старта(self, pid, *a):
        return self._вход

    def подписочный_ключ(self, *a):
        return "proba-klyuch-login"


def main() -> int:
    for путь in (SWITCH, REPORT, HOOK):
        if not путь.is_file():
            print(f"ПРОБА СЛОМАНА: нет {путь}")
            return 1
    failed = total = 0

    def case(name, ok, got):
        nonlocal failed, total
        total += 1
        failed += not ok
        print(f"  {'ок  ' if ok else 'ПЛОХО'} {name}" + ("" if ok else f": получили {got!r}"))

    def без_ключей(текст):
        return not any(к in текст for к in (*КЛЮЧИ.values(), "proba-klyuch-login", *(f"key-{и}" for и in КЛЮЧИ)))

    with tempfile.TemporaryDirectory(prefix="o-acc-") as tmp:
        корень = Path(tmp)
        env = стенд(корень, идёт="alfa", запуск="beta")
        os.environ.update({к: env[к] for к in ("HARNESS_ACCOUNT_DIR", "SECRETS_DIR", "LOG_DIR", "HARNESS_CONF",
                                                "HARNESS_HOME", "HARNESS_INSTALL_CONF")})
        cle = load("cle_now", LOGIN)
        cle.ПОТОКИ = str(корень / "log" / "demon-stream")
        sw = load("switch_now", SWITCH)
        читатель = getattr(sw, "аккаунты_сейчас", None)
        case("account-switch.py: читатель аккаунты_сейчас есть", callable(читатель), None)
        if callable(читатель):
            строка = читатель(Сторож(cle, pid=4242, ключ=КЛЮЧИ["alfa"]))
            case("процесс смены на alfa, запуск beta — названы оба и расхождение",
                 строка.startswith("аккаунт: ") and "подпись-alfa" in строка and "подпись-beta" in строка
                 and "расход" in строка and "помощник" in строка, строка)
            case("смена — та, что несёт процесс: alfa раньше beta",
                 "подпись-alfa" in строка and строка.find("подпись-alfa") < строка.find("подпись-beta"), строка)

            стенд_2 = стенд(корень / "s2", идёт="gamma", запуск="gamma")
            os.environ["HARNESS_ACCOUNT_DIR"] = стенд_2["HARNESS_ACCOUNT_DIR"]
            строка = читатель(Сторож(cle, pid=4242, ключ=КЛЮЧИ["gamma"]))
            case("одна запись на всех — одна подпись, без «расход»",
                 строка.count("подпись-gamma") == 1 and "расход" not in строка and "демон" in строка, строка)

            строка = читатель(Сторож(cle, pid=4242, ключ=КЛЮЧИ["gamma"], вход_в_сессии=True))
            case("«/login» внутри сессии — названо словами, state.json не выдан за факт",
                 "/login" in строка and "сессии" in строка, строка)

            строка = читатель(Сторож(cle, pid=None))
            case("процесса смены нет — по записи state.json, так и сказано",
                 "подпись-gamma" in строка and "state.json" in строка, строка)

            поток(корень / "s2", "a.jsonl", отпечаток(КЛЮЧИ["alfa"]), 60)
            поток(корень / "s2", "b.jsonl", отпечаток(КЛЮЧИ["gamma"]), 600)
            cle.ПОТОКИ = str(корень / "s2" / "log" / "demon-stream")
            строка = читатель(Сторож(cle, pid=4242, ключ=КЛЮЧИ["gamma"]))
            case("последний вызов демона шёл не на профиле запуска — сказано, на каком",
                 "подпись-alfa" in строка and "демон" in строка, строка)
            поток(корень / "s2", "c.jsonl", отпечаток(КЛЮЧИ["gamma"]), 5)
            строка = читатель(Сторож(cle, pid=4242, ключ=КЛЮЧИ["gamma"]))
            case("последний вызов демона на профиле запуска — лишнего не сказано",
                 "подпись-alfa" not in строка, строка)
            case("читатель: ключей и имён файлов ключей в строке нет", без_ключей(строка), строка)

        # ── CLI и печатающие места: свой процесс, свой стенд ──
        env = стенд(корень / "s3", идёт="alfa", запуск="beta")
        cli = subprocess.run([sys.executable, str(SWITCH), "--сейчас"], env=env, capture_output=True,
                             text=True, timeout=60)
        строки = cli.stdout.strip().splitlines()
        case("--сейчас: одна строка «аккаунт: …», код 0",
             cli.returncode == 0 and len(строки) == 1 and строки[0].startswith("аккаунт: ")
             and "подпись-alfa" in строки[0] and "подпись-beta" in строки[0], (cli.returncode, cli.stdout, cli.stderr[-300:]))
        case("--сейчас: ключей в выводе нет", без_ключей(cli.stdout + cli.stderr), cli.stdout + cli.stderr)

        env_пусто = стенд(корень / "s4", профили=False)
        cli = subprocess.run([sys.executable, str(SWITCH), "--сейчас"], env=env_пусто, capture_output=True,
                             text=True, timeout=60)
        case("--сейчас без профилей: молчит, код 3", cli.returncode == 3 and not cli.stdout.strip(),
             (cli.returncode, cli.stdout))

        # Статус в канал. Копия под мутацией лежит не рядом с дверью: дверь ищется рядом с ОТЧЁТОМ.
        (корень / "s3" / "dev-map.yaml").write_text("{}\n", encoding="utf-8")
        (корень / "s3" / "queue.txt").write_text("", encoding="utf-8")
        (корень / "s3" / "j.jsonl").write_text("", encoding="utf-8")
        отчёт = subprocess.run(
            [sys.executable, str(REPORT), "--журнал-вызовов", str(корень / "s3" / "j.jsonl"),
             "--карта", str(корень / "s3" / "dev-map.yaml"), "--очередь", str(корень / "s3" / "queue.txt")],
            env={**env, "PROJECT_DIR": str(корень / "s3")}, capture_output=True, text=True, timeout=120,
            cwd=str(корень / "s3"))
        есть = [s for s in отчёт.stdout.splitlines() if s.startswith("аккаунт: ")]
        case("status-report.py: строка «аккаунт: …» в статусе",
             отчёт.returncode == 0 and len(есть) == 1 and "подпись-beta" in есть[0],
             (отчёт.returncode, отчёт.stdout[-400:], отчёт.stderr[-300:]))
        отчёт = subprocess.run(
            [sys.executable, str(REPORT), "--журнал-вызовов", str(корень / "s3" / "j.jsonl"),
             "--карта", str(корень / "s3" / "dev-map.yaml"), "--очередь", str(корень / "s3" / "queue.txt")],
            env={**env_пусто, "PROJECT_DIR": str(корень / "s3")}, capture_output=True, text=True, timeout=120,
            cwd=str(корень / "s3"))
        case("status-report.py без профилей: строки нет",
             отчёт.returncode == 0 and "аккаунт:" not in отчёт.stdout, (отчёт.returncode, отчёт.stdout[-300:]))

        # Старт смены: проект стенда — каталог со ссылкой scripts на настоящие скрипты.
        проект = корень / "s3" / "proj"
        проект.mkdir()
        (проект / "scripts").symlink_to(SWITCH.parent)
        (корень / "s3" / "install.conf").write_text(f'PROJECT_DIR="{проект}"\nPROJECT_NAME="proba"\n',
                                                    encoding="utf-8")
        крюк = subprocess.run(["bash", str(HOOK)], env={**env, "HOME": str(корень / "s3"),
                                                       "SHIFT_SELF_MARK": str(корень / "s3" / "shift.mark")},
                              capture_output=True, text=True, timeout=180, cwd=str(проект), stdin=subprocess.DEVNULL)
        есть = [s for s in крюк.stdout.splitlines() if s.startswith("аккаунт: ")]
        case("session_state.sh: строка «аккаунт: …» на старте смены",
             len(есть) == 1 and "подпись-alfa" in есть[0] and "подпись-beta" in есть[0],
             (крюк.returncode, крюк.stdout[-600:], крюк.stderr[-300:]))
        case("session_state.sh: ключей в выводе нет", без_ключей(крюк.stdout + крюк.stderr), "ключ в выводе")

    print(f"итого путей {total}, неудач {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
