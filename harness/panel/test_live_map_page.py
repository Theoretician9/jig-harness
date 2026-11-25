#!/usr/bin/env python3
"""Проверка маршрутов живой карты: /puls, /puls/app.js, /puls/snimok, вход.

Спека: спека живой карты (.tasks/<задача>/спека.md) §5.6 (состояния), §6
(страница, CSP, отметка зрителя), §7 (вход `web_app` и запасной `dalee`),
К-6, К-7(г), К-8. План — Task 5 (и кнопка «Живая карта» на панели — Task 6).

Служба поднимается на свободном порту; каталог состояния панели
(`PANEL_STATE_DIR`), снимок (`PANEL_LIVE_MAP`), журналы (`LOG_DIR`) и секреты
входа — свои, во временном каталоге. Ключ Telegram — пара стенда: открытый
ключ лежит в `TG_WEBAPP_PUBKEY` конфига пробы, данные запуска подписывает
закрытый ключ стенда. Боевой снимок, боевая служба и ключ Telegram не
трогаются: проверка СОЗДАЁТ своё условие.

Дверь пробы: `PANEL_SERVER_DIR` — каталог службы (server.py, state.py, vid.py,
tg_web_app.py, live_map.js), по умолчанию — каталог этой пробы. Мутации
судятся на копии каталога; копия лежит как <корень>/harness/panel рядом с
копией <корень>/scripts/lib — оттуда settings.py берёт загрузчик конфига.

Прогон:
    python3 harness/panel/test_live_map_page.py
"""
import base64
import datetime
import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

SERVER_DIR = Path(os.environ.get("PANEL_SERVER_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(SERVER_DIR))

путей = 0
неудач = 0


def путь_ок(текст):
    global путей
    путей += 1
    print("ок  · " + текст)


def путь_плохо(текст):
    global путей, неудач
    путей += 1
    неудач += 1
    print("ПЛОХО · " + текст)


def итог_и_выход():
    print(f"ЖИВАЯ-КАРТА-СТРАНИЦА: путей {путей}, неудач {неудач}")
    sys.exit(1 if неудач else 0)


def судить(условие: bool, имя: str, иначе: str) -> None:
    if условие:
        путь_ок(имя)
    else:
        путь_плохо(f"{имя}: {иначе}")


# Отправка в канал выключена до импорта службы: вход пробы иначе написал бы
# владельцу настоящее сообщение.
os.environ["PANEL_TG_SEND"] = ""

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError as беда:
    путь_плохо(f"нет cryptography у интерпретатора пробы (Н-9): {беда}")
    итог_и_выход()

try:
    import server
except Exception as беда:   # служба не поднимается — это ответ, а не трассировка
    путь_плохо(f"службы панели нет в {SERVER_DIR}: {беда}")
    итог_и_выход()

OWNER_ID = "100000001"
BOT_ID = "777000111"
# Порог входа меньше боевых 12 ч НАРОЧНО: путь «возраст 2 ч — 401» краснеет,
# если служба вшила 43200 вместо чтения LIVE_MAP_TG_AUTH_S из конфига.
AUTH_S = 3600
KEY = Ed25519PrivateKey.generate()
PUBKEY_HEX = KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
LOGIN_TEXT = "Войдите: нажмите «Карта» в чате ещё раз"
TG_FRAME = "frame-ancestors 'self' https://web.telegram.org"
NO_FRAME = "frame-ancestors 'none'"
BROWSER = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}

HARNESS_CONF_TEXT = f"""PUSH_NEEDS_OWNER=0
DEPLOY_NEEDS_OWNER=1
PUBLISH_NEEDS_OWNER=1
LIVE_MAP_TG_AUTH_S={AUTH_S}
TG_BOT_ID="{BOT_ID}"
TG_WEBAPP_PUBKEY="{PUBKEY_HEX}"
"""


def свободный_порт() -> int:
    с = socket.socket()
    с.bind(("127.0.0.1", 0))
    порт = с.getsockname()[1]
    с.close()
    return порт


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def init_data(*, user_id=OWNER_ID, age_s=60, bot_id=BOT_ID, spoil=False) -> str:
    """Данные запуска мини-приложения, подписанные ключом стенда (§7, Н-1)."""
    pairs = {
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": int(user_id), "first_name": "Владелец"},
                           ensure_ascii=False, separators=(",", ":")),
        "auth_date": str(int(time.time() - age_s)),
    }
    check = f"{bot_id}:WebAppData\n" + "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    signature = bytearray(KEY.sign(check.encode("utf-8")))
    if spoil:
        signature[1] ^= 0x01
    pairs["signature"] = b64url(bytes(signature))
    pairs["hash"] = "0" * 64
    return urllib.parse.urlencode(pairs)


def iso_ago(seconds: float) -> str:
    moment = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds")


def snapshot(period_s: int, age_s: float, **поля) -> dict:
    """Снимок по §5.5: форма та же, что у образца scripts/fixtures/live-map."""
    data = {
        "v": 1, "снято": iso_ago(age_s), "период_с": period_s, "окно_с": 60,
        "сборка_мс": 42,
        "машина": {"load1": 1.5, "ядер": 8, "места": {"занято": 1, "всего": 4}},
        "токены": None,
        "части": [{"id": "channel", "имя": "Канал", "механизмов": 3, "событий": 1,
                   "исходы": {"ok": 1, "refused": 0, "error": 0}, "доля_ядра": 0.01,
                   "последнее": None}],
        "внешние": [{"id": "внешний:владелец", "имя": "Владелец"}],
        "рёбра": [{"от": "внешний:владелец", "к": "channel", "что": "сообщения",
                   "источник": True, "событий": 1, "последнее": None}],
        "события": [], "исполнители": [],
        "без_места": {"событий": 0, "src": []},
        "источники": [{"имя": "tokens-2026-11", "возраст_с": None,
                       "беда": "файла журнала нет"}],
    }
    data.update(поля)
    return data


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    panel_state = root / "панель"
    logs = root / "логи"
    for каталог in (panel_state, logs / "inbox" / "обработано", root / "секреты"):
        каталог.mkdir(parents=True, exist_ok=True)
    (root / "harness.conf").write_text(HARNESS_CONF_TEXT, encoding="utf-8")
    (root / "install.conf").write_text(f'TG_CHAT_ID="{OWNER_ID}"\nLOG_DIR="{logs}"\n',
                                       encoding="utf-8")
    live_map = panel_state / "live-map.json"
    viewer = panel_state / "live-map.viewer"
    os.environ.update(
        HARNESS_CONF=str(root / "harness.conf"),
        HARNESS_INSTALL_CONF=str(root / "install.conf"),
        LOG_DIR=str(logs),
        SECRETS_DIR=str(root / "секреты"),
        PANEL_STATE_DIR=str(panel_state),
        PANEL_LIVE_MAP=str(live_map),
        PANEL_BOARD=str(root / "нет-доски.json"),
        PANEL_DEVMAP=str(root / "нет-карты.yaml"),
    )

    port = свободный_порт()
    server.Панель.log_message = lambda self, f, *a: sys.stderr.write((f % a) + "\n")
    service = server.собрать(порт=port)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    time.sleep(0.3)

    def get(path: str, headers: dict | None = None) -> tuple:
        """(код, заголовки, тело) без перехода по 303: Location и Set-Cookie
        судятся сами. Лимит частоты сбрасывается: проба быстрее человека."""
        server.ЧАСТОТА_ЧТЕНИЯ.следы.clear()
        server.ЧАСТОТА_ВХОДА.следы.clear()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return (response.status, {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode("utf-8", "replace"))
        except (OSError, http.client.HTTPException) as error:
            return 0, {}, f"нет ответа: {error}"
        finally:
            conn.close()

    def as_json(body: str):
        try:
            return json.loads(body)
        except ValueError:
            return None

    def write_snapshot(data) -> None:
        live_map.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    OLD_MTIME = time.time() - 1000

    def reset_viewer() -> None:
        """Отметка зрителя старая: любое её касание видно по mtime."""
        viewer.touch()
        os.utime(viewer, (OLD_MTIME, OLD_MTIME))

    def viewer_untouched() -> bool:
        return abs(viewer.stat().st_mtime - OLD_MTIME) < 1

    def viewer_fresh() -> bool:
        return time.time() - viewer.stat().st_mtime <= 5

    def enter(dalee: str | None) -> tuple:
        """Вход по ссылке из чата так, как его делает браузер владельца."""
        token = server.вход().выдать_токен(OWNER_ID)
        query = f"t={token}" + ("" if dalee is None else "&dalee=" + dalee)
        code, headers, _ = get(f"/vhod?{query}", BROWSER)
        return code, headers.get("location"), headers.get("set-cookie", "")

    write_snapshot(snapshot(60, 59))

    # ── К-6: без пропуска ───────────────────────────────────────────────────
    code, headers, body = get("/puls")
    судить(code == 401, "БОЛЬНОЙ: без пропуска /puls → 401", f"вышло {code} {body[:80]!r}")
    судить(headers.get("content-type", "").startswith("text/html"),
           "без пропуска /puls — HTML, а не JSON (§5.6 п. 7)",
           f"Content-Type {headers.get('content-type')!r}")
    судить(LOGIN_TEXT in body, f"без пропуска /puls — в теле «{LOGIN_TEXT}»",
           f"тело {body[:120]!r}")
    судить('<script src="/puls/app.js"></script>' in body,
           "страница входа несёт тот же скрипт /puls/app.js (Н-4)",
           "тега <script src=\"/puls/app.js\"></script> нет")
    # Скрипт карты снимает «Войдите», очищая корень #karta: текст вне корня
    # остался бы на экране после входа заголовком (Н-4, проба node).
    судить(re.search(r'<div id="karta"[^>]*>.*?Войдите', body, re.S) is not None,
           "текст «Войдите» стоит внутри <div id=\"karta\"> — скрипт его снимет",
           "внутри <div id=\"karta\"> текста «Войдите» нет")
    csp = headers.get("content-security-policy", "")
    судить(TG_FRAME in csp, "у /puls (и на 401) frame-ancestors для Telegram Web",
           f"CSP {csp!r}")
    судить("default-src 'self'" in csp and "unsafe-inline" not in csp.split("style-src")[0],
           "у /puls CSP по-прежнему default-src 'self', скриптам inline не открыт",
           f"CSP {csp!r}")

    reset_viewer()
    code, headers, body = get("/puls/snimok")
    судить(code == 401, "БОЛЬНОЙ: без пропуска /puls/snimok → 401", f"вышло {code}")
    судить(as_json(body) == {"беда": "нужен пропуск"},
           "без пропуска /puls/snimok — JSON {\"беда\": \"нужен пропуск\"}", f"тело {body[:120]!r}")
    судить(viewer_untouched(), "БОЛЬНОЙ: без пропуска отметка зрителя не тронута (М-5)",
           "mtime live-map.viewer изменился — гость будит частый режим")
    судить(TG_FRAME in headers.get("content-security-policy", ""),
           "у /puls/snimok frame-ancestors для Telegram Web",
           f"CSP {headers.get('content-security-policy')!r}")

    reset_viewer()
    code, _, _ = get("/puls/snimok", {"Cookie": f"{server.ИМЯ_ПРОПУСКА}=poddelka-0000"})
    судить(code == 401 and viewer_untouched(),
           "БОЛЬНОЙ: поддельный пропуск — 401, отметка не тронута",
           f"вышло {code}, отметка тронута: {not viewer_untouched()}")

    code, headers, body = get("/puls/app.js")
    js_file = SERVER_DIR / "live_map.js"
    судить(code == 200, "/puls/app.js без пропуска → 200 (в нём нет данных, §6)",
           f"вышло {code} {body[:80]!r}")
    судить(headers.get("content-type") == "text/javascript; charset=utf-8",
           "/puls/app.js — text/javascript; charset=utf-8",
           f"Content-Type {headers.get('content-type')!r}")
    судить(js_file.is_file() and body == js_file.read_text(encoding="utf-8"),
           "/puls/app.js отдаёт файл live_map.js рядом со службой",
           f"тело не равно {js_file} (файл есть: {js_file.is_file()})")
    судить(TG_FRAME in headers.get("content-security-policy", ""),
           "у /puls/app.js frame-ancestors для Telegram Web",
           f"CSP {headers.get('content-security-policy')!r}")

    # ── К-6: прочие пути остаются под запретом обрамления ───────────────────
    for path in ("/", "/karta", "/vhod?t=net"):
        code, headers, _ = get(path)
        csp = headers.get("content-security-policy", "")
        судить(NO_FRAME in csp and "web.telegram.org" not in csp,
               f"у {path} frame-ancestors 'none' (ослабление только для /puls*)",
               f"CSP {csp!r}")

    # ── К-6: вход Telegram заголовком ───────────────────────────────────────
    def tma(path: str, data: str) -> tuple:
        return get(path, {"Authorization": f"tma {data}"})

    больные = [
        ("чужой user.id", init_data(user_id="100000002")),
        ("испорчена подпись", init_data(spoil=True)),
        ("auth_date старше 12 ч", init_data(age_s=43200 + 120)),
        (f"auth_date старше LIVE_MAP_TG_AUTH_S={AUTH_S} (2 ч) — ключ из конфига",
         init_data(age_s=7200)),
        ("подпись для другого бота — TG_BOT_ID из конфига", init_data(bot_id="999000222")),
        ("пустые данные tma", ""),
    ]
    for имя, data in больные:
        reset_viewer()
        code, _, body = tma("/puls/snimok", data)
        судить(code == 401 and viewer_untouched(),
               f"БОЛЬНОЙ: tma, {имя} — 401, отметка не тронута",
               f"вышло {code}, отметка тронута: {not viewer_untouched()}, тело {body[:80]!r}")
    reset_viewer()
    code, _, _ = get("/puls/snimok", {"Authorization": f"Bearer {init_data()}"})
    судить(code == 401 and viewer_untouched(),
           "БОЛЬНОЙ: верные данные, но схема не tma — 401, отметка не тронута",
           f"вышло {code}, отметка тронута: {not viewer_untouched()}")

    reset_viewer()
    code, _, body = tma("/puls/snimok", init_data())
    судить(code == 200, "tma с подписью стенда — 200 без куки (§7)",
           f"вышло {code} {body[:120]!r}")
    судить(viewer_fresh(), "вход tma — отметка зрителя свежая (≤ 5 с)",
           "mtime live-map.viewer не обновлён")

    # ── вход по ссылке и белый список dalee ─────────────────────────────────
    code, location, cookie = enter("/puls")
    судить(code == 303 and location == "/puls",
           "вход /vhod?t=…&dalee=/puls ведёт на /puls", f"вышло {code} {location!r}")
    pass_value = ""
    match = re.match(rf"{re.escape(server.ИМЯ_ПРОПУСКА)}=([^;]+)", cookie)
    if match:
        pass_value = match.group(1)
        путь_ок("вход с dalee выдал пропуск")
    else:
        путь_плохо(f"вход с dalee не выдал пропуска: Set-Cookie {cookie[:80]!r}")
    for dalee in ("//evil.example", "https://evil.example/puls", "%2F%2Fevil.example",
                  "/pulsar", "/puls/../nastrojki", "/karta"):
        code, location, _ = enter(dalee)
        судить(code == 303 and location == "/",
               f"БОЛЬНОЙ: dalee={dalee} — Location: / (белый список из одного /puls)",
               f"вышло {code} {location!r}")
    code, location, _ = enter(None)
    судить(code == 303 and location == "/", "без dalee вход по-прежнему ведёт на /",
           f"вышло {code} {location!r}")

    if not pass_value:
        путь_плохо("пропуска нет — пути с пропуском не судятся")
        service.shutdown()
        итог_и_выход()
    with_pass = {"Cookie": f"{server.ИМЯ_ПРОПУСКА}={pass_value}"}

    # ── К-6: с пропуском ────────────────────────────────────────────────────
    code, headers, body = get("/puls", with_pass)
    судить(code == 200 and headers.get("content-type", "").startswith("text/html"),
           "с пропуском /puls → 200 text/html", f"вышло {code} {headers.get('content-type')!r}")
    судить('<div id="karta"' in body and '<script src="/puls/app.js"></script>' in body,
           "страница карты — корень <div id=\"karta\"> и скрипт /puls/app.js",
           f"тело {body[:160]!r}")
    inline = [t for t in re.findall(r"<script\b[^>]*>(.*?)</script>", body, re.S) if t.strip()]
    no_src = re.findall(r"<script\b(?![^>]*\bsrc=)[^>]*>", body)
    судить(not inline and not no_src, "на странице нет встроенного скрипта (CSP 'self')",
           f"встроенных: {len(inline) + len(no_src)}")
    внешние = re.findall(r'(?:src|href)="(?:https?:)?//', body)
    судить(not внешние, "страница не грузит ничего извне (нет CDN)",
           f"внешних ссылок: {len(внешние)}")
    # §5.6 п. 3: устаревшая карта серая, а плашка над ней не сдвигает её на треть экрана.
    серая = re.search(r"#karta svg\.stale\{[^}]*filter:grayscale\(1\)", body)
    плашка = re.search(r"#karta p\.banner\{[^}]*margin:8px 0", body)
    судить(bool(серая and плашка), "стиль: svg.stale — серый (grayscale), p.banner — margin 8px 0",
           f"svg.stale {bool(серая)}, p.banner {bool(плашка)}")
    судить(TG_FRAME in headers.get("content-security-policy", ""),
           "у /puls с пропуском frame-ancestors для Telegram Web",
           f"CSP {headers.get('content-security-policy')!r}")

    code, _, body = get("/", with_pass)
    судить(code == 200 and '<a href="/puls"><button>Живая карта</button></a>' in body,
           "на панели кнопка «Живая карта» ведёт на /puls (Task 6)",
           "кнопки <a href=\"/puls\"><button>Живая карта</button></a> нет")

    reset_viewer()
    sent = snapshot(60, 59)
    write_snapshot(sent)
    code, headers, body = get("/puls/snimok", with_pass)
    got = as_json(body) or {}
    судить(code == 200 and headers.get("content-type", "").startswith("application/json"),
           "с пропуском /puls/snimok → 200 JSON", f"вышло {code} {body[:120]!r}")
    судить(viewer_fresh(), "с пропуском отметка зрителя свежая (≤ 5 с)",
           "mtime live-map.viewer не обновлён — сборщик не перейдёт в частый режим")
    судить(set(got) == set(sent) | {"устарел"},
           "сервер добавляет к снимку ровно одно поле — устарел",
           f"лишние {sorted(set(got) - set(sent) - {'устарел'})}, "
           f"пропавшие {sorted(set(sent) - set(got))}")
    судить(got.get("v") == 1 and got.get("части") == sent["части"]
           and got.get("рёбра") == sent["рёбра"],
           "снимок отдан как есть: v, части, рёбра", f"вышло {str(got)[:160]}")

    # ── К-7(г): порог «устарел» = 3 × период_с САМОГО снимка ───────────────
    def stale(period_s: int, age_s: float):
        write_snapshot(snapshot(period_s, age_s))
        _, _, body = get("/puls/snimok", with_pass)
        return (as_json(body) or {}).get("устарел", "нет поля")

    for period_s, age_s, ждём in ((60, 59, False), (60, 181, True),
                                  (5, 12, False), (5, 20, True)):
        вышло = stale(period_s, age_s)
        судить(вышло is ждём,
               f"период_с {period_s}, возраст {age_s} с — устарел: {str(ждём).lower()}",
               f"вышло {вышло!r} (порог — 3 × период_с самого снимка, К-7(г))")

    # ── §5.6 п. 6: реестра частей нет — беда как есть, частей не выдумано ───
    write_snapshot(snapshot(60, 10, беда="нет architecture.yaml", части=[], рёбра=[]))
    code, _, body = get("/puls/snimok", with_pass)
    got = as_json(body) or {}
    судить(code == 200 and got.get("беда") == "нет architecture.yaml" and got.get("части") == [],
           "реестра нет — 200, беда передана, частей 0", f"вышло {code} {body[:120]!r}")

    # ── К-8: снимка нет — 503 и «ещё не собрана» ────────────────────────────
    live_map.unlink()
    code, headers, body = get("/puls/snimok", with_pass)
    судить(code == 503, "снимка нет — 503 (§5.6 п. 1)", f"вышло {code} {body[:80]!r}")
    судить("ещё не собрана" in body, "снимка нет — в теле «ещё не собрана»",
           f"тело {body[:120]!r}")
    судить(headers.get("content-type", "").startswith("application/json"),
           "снимка нет — ответ JSON (его читает скрипт)", f"{headers.get('content-type')!r}")

    service.shutdown()

итог_и_выход()
