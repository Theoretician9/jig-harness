#!/usr/bin/env python3
"""Проба ручки PANE_SEND_NOW в панели (карточка hr.pane-send-now-switch).

Ручка решает, отдаёт ли диспетчер толчок модели СРАЗУ (аккорд Ctrl+X Ctrl+S),
или сообщение ждёт конца её хода. Спека: .tasks/hr.pane-send-now-switch/spec.md.

Служба поднимается на свободном порту во временном каталоге, реестр ручек —
НАСТОЯЩИЙ harness/config/switches.yaml: проба судит ту строку, которую увидит
владелец, а не свою копию. Вход и запись — тем же путём, что test_server.py.

Прогон:
    python3 harness/panel/test_send_now_switch.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ПАНЕЛЬ = Path(__file__).resolve().parent
КОРЕНЬ = ПАНЕЛЬ.parent.parent
sys.path.insert(0, str(ПАНЕЛЬ))

import yaml  # noqa: E402

КЛЮЧ = "PANE_SEND_NOW"
путей = 0
неудач = 0


def итог(годно: bool, текст: str):
    global путей, неудач
    путей += 1
    if not годно:
        неудач += 1
    print(("ок  · " if годно else "ПЛОХО · ") + текст)


def свободный_порт() -> int:
    с = socket.socket()
    с.bind(("127.0.0.1", 0))
    порт = с.getsockname()[1]
    с.close()
    return порт


def запрос(url, данные=None, заголовки=None):
    тело = json.dumps(данные).encode() if данные is not None else None
    req = urllib.request.Request(url, data=тело, headers=заголовки or {})
    if тело is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as ответ:
            return ответ.status, ответ.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as беда:
        return беда.code, беда.read().decode("utf-8", "replace")


def ключ_в_файле(файл: Path) -> str | None:
    for строка in файл.read_text(encoding="utf-8").splitlines():
        if строка.startswith(f"{КЛЮЧ}="):
            return строка.split("=", 1)[1].strip().strip('"\'')
    return None


# ── 1. строка реестра по образцу безопасной ручки ──────────────────────────
реестр = yaml.safe_load((КОРЕНЬ / "harness/config/switches.yaml").read_text(encoding="utf-8"))
строка = next((с for с in реестр.get("переключатели") or [] if с.get("ключ") == КЛЮЧ), None)
итог(строка is not None, f"ручка {КЛЮЧ} есть в реестре switches.yaml")
строка = строка or {}
итог(строка.get("файл") == "harness.conf" and строка.get("вид") == "выбор",
     f"ручка живёт в harness.conf и это выбор: {строка.get('файл')} / {строка.get('вид')}")
значения = {str(з.get("значение")): з for з in строка.get("значения") or []}
итог(set(значения) == {"0", "1"} and значения.get("0", {}).get("по_умолчанию") is True,
     f"значения ровно 0 и 1, умолчание 0: {sorted(значения)}")
итог(строка.get("опасный") is False,
     "ручка не опасная: обратима и данных не трогает (подтверждение в боте не нужно)")
итог(строка.get("читатель") == "scripts/tg-dispatcher.sh"
     and строка.get("способ_чтения") == "при_вызове" and строка.get("применяется") == "сразу",
     "читатель — диспетчер, читается при вызове, применяется сразу: "
     f"{строка.get('читатель')} / {строка.get('способ_чтения')} / {строка.get('применяется')}")
образец = КОРЕНЬ / "harness/config/harness.conf"
итог(ключ_в_файле(образец) == "0", f"образец harness.conf несёт {КЛЮЧ}=0 (приедет на новую машину)")

# ── 2. живая служба: ручка видна и пишется ─────────────────────────────────
try:
    import server
except ImportError as беда:
    итог(False, f"службы панели нет: {беда}")
    print(f"РУЧКА-СРАЗУ: путей {путей}, неудач {неудач}")
    sys.exit(1)

with tempfile.TemporaryDirectory() as tmp:
    врем = Path(tmp)
    конфиг = врем / "harness.conf"
    конфиг.write_text(f'CTX_ROTATE_PCT=75\n{КЛЮЧ}="0"\n', encoding="utf-8")
    (врем / "install.conf").write_text('TG_CHAT_ID="100000001"\n', encoding="utf-8")
    (врем / "логи").mkdir()
    (врем / "секреты").mkdir()
    os.environ.update(HARNESS_CONF=str(конфиг),
                      HARNESS_INSTALL_CONF=str(врем / "install.conf"),
                      LOG_DIR=str(врем / "логи"),
                      SECRETS_DIR=str(врем / "секреты"))
    os.environ.pop("HARNESS_PERECLUCHATELI", None)

    порт = свободный_порт()
    server.Панель.log_message = lambda self, ф, *а: sys.stderr.write((ф % а) + "\n")
    служба = server.собрать(порт=порт)
    threading.Thread(target=служба.serve_forever, daemon=True).start()
    time.sleep(0.3)
    корень = f"http://127.0.0.1:{порт}"
    свой = {"Origin": корень, "Sec-Fetch-Site": "same-origin"}

    токен = server.вход().выдать_токен("100000001")
    код, тело = запрос(f"{корень}/vhod", данные={"t": токен}, заголовки=свой)
    пропуск = json.loads(тело).get("пропуск") if код == 200 else None
    итог(bool(пропуск), f"условие создано: вход дал пропуск ({код})")
    с_пропуском = dict(свой, Cookie=f"panel={пропуск}")

    код, тело = запрос(f"{корень}/nastrojki", заголовки=с_пропуском)
    настройки = json.loads(тело) if код == 200 else {}
    ручка = next((с for с in настройки.get("переключатели", []) if с.get("ключ") == КЛЮЧ), None)
    итог(ручка is not None and ручка.get("сейчас") == "0" and ручка.get("состояние") == "значение",
         f"/nastrojki отдаёт ручку с текущим значением 0: {ручка and ручка.get('сейчас')}")
    csrf = настройки.get("csrf")

    код, тело = запрос(f"{корень}/", заголовки=с_пропуском)
    итог(код == 200 and f'name="ключ" value="{КЛЮЧ}"' in тело,
         "главная страница панели рисует кнопку этой ручки")

    for значение in ("1", "0"):
        код, тело = запрос(f"{корень}/kljuch", заголовки=с_пропуском,
                           данные={"ключ": КЛЮЧ, "значение": значение, "csrf": csrf})
        итог(код == 200 and ключ_в_файле(конфиг) == значение,
             f"кнопка ставит {значение}: ответ {код}, в harness.conf {ключ_в_файле(конфиг)!r}")

    было = конфиг.read_text(encoding="utf-8")
    код, тело = запрос(f"{корень}/kljuch", заголовки=с_пропуском,
                       данные={"ключ": КЛЮЧ, "значение": "2", "csrf": csrf})
    итог(код == 422 and конфиг.read_text(encoding="utf-8") == было,
         f"негодное «2» — отказ 422, файл цел: {код}")

    служба.shutdown()

print(f"РУЧКА-СРАЗУ: путей {путей}, неудач {неудач}")
sys.exit(1 if неудач else 0)
