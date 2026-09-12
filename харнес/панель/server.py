#!/usr/bin/env python3
"""Служба веб-панели харнеса: переключатели владельцу, без прав и без лишнего.

Владелец 11.09.2026: «нам нужно будет сейчас сделать веб панель для управления
харнесом, куда надо будет вывести как раз таки все переключатели»; «Панель
должна быть очень безопасной, что хрен взломаешь».

Почему только стандартная библиотека: панель обязана ставиться на чистой машине
без pip и не тянуть цепочку чужих обновлений — это часть требования
безопасности. Нагрузка — один человек.

Что панель НЕ умеет, и это намеренно:
  • исполнять команды и принимать пути файлов — её вход это пара «ключ из
    реестра, значение из перечня», инъектировать нечего;
  • писать в конфиг сама — запись делает `scripts/zapisat-klyuch.py`, которому
    она разрешена одной строкой sudoers (находка C5 ревью);
  • применять ОПАСНЫЙ переключатель без подтверждения в канале (C3): такие
    запросы получают отказ 409 и уходят в ожидание.

Слушает только 127.0.0.1: наружу ведёт отдельный server-блок nginx со своим
именем (C6 — общий origin с продуктом отдал бы панель чужому скрипту).

Прогон вручную:
    LOG_DIR=/var/log/harness python3 харнес/панель/server.py --порт 8787
"""
import html
import json
import os
import secrets
import subprocess
import sys
import urllib.parse
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402  (после sys.path — модуль лежит рядом)

import podtverzhdenie  # noqa: E402
import sostoyanie  # noqa: E402
import vid  # noqa: E402
import vhod  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
РЕЕСТР = Path(os.environ.get("HARNESS_PERECLUCHATELI")
              or КОРЕНЬ / "харнес" / "config" / "переключатели.yaml")
ПИСАТЕЛЬ = КОРЕНЬ / "scripts" / "zapisat-klyuch.py"
def _конфиги() -> dict:
    return {
        "install.conf": os.environ.get("HARNESS_INSTALL_CONF", КОНФИГИ["install.conf"]),
        "harness.conf": os.environ.get("HARNESS_CONF", КОНФИГИ["harness.conf"]),
    }


КОНФИГИ = {
    "install.conf": os.environ.get("HARNESS_INSTALL_CONF", "/etc/harness/install.conf"),
    "harness.conf": os.environ.get("HARNESS_CONF", "/etc/harness/harness.conf"),
}
def состояние_дир() -> Path:
    """Куда панель пишет свой журнал и ожидания. См. podtverzhdenie._лог_дир."""
    return Path(os.environ.get("PANEL_STATE_DIR")
                or os.environ.get("LOG_DIR", "/var/log/harness"))
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/var/lib/harness/secrets"))

# Имя пропуска в cookie — ЛАТИНИЦЕЙ: заголовки HTTP кодируются latin-1, и
# кириллическое имя роняет ответ исключением внутри обработчика (пойман
# проверкой 11.09.2026 — соединение закрывалось без ответа).
ИМЯ_ПРОПУСКА = "panel"

# Что закрыто для сессии, вошедшей запасным кодом: она урезанная по замыслу —
# подтвердить опасное в боте при мёртвом канале некому.
ЗАКРЫТО_ЗАПАСНОЙ = ("/shag", "/pamyat", "/nabor")

# Лимиты частоты: вход — редкое действие, чтение состояния — частое.
ПОПЫТОК_ВХОДА_В_МИНУТУ = 5
ЗАПРОСОВ_В_МИНУТУ = 60


def _чит_конфиг(путь: str) -> dict:
    """Мини-разбор shell-конфига. Не исполняем: панели нельзя исполнять чужое."""
    значения = {}
    try:
        текст = Path(путь).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return значения
    for строка in текст.splitlines():
        строка = строка.strip()
        if not строка or строка.startswith("#") or "=" not in строка:
            continue
        ключ, _, значение = строка.partition("=")
        значения[ключ.strip()] = значение.split("#")[0].strip().strip('"\'')
    return значения


_ВХОД = None


def вход() -> vhod.Вход:
    """Вход заводится при первом обращении, а не при импорте: пути берутся из
    окружения, и служба (как и проверка) вправе выставить их до первого запроса."""
    global _ВХОД
    if _ВХОД is None:
        _ВХОД = vhod.Вход(
            каталог=vhod.каталог_секретов(),
            chat_id=_чит_конфиг(os.environ.get("HARNESS_INSTALL_CONF")
                                or КОНФИГИ["install.conf"]).get("TG_CHAT_ID", ""))
    return _ВХОД


def реестр() -> dict:
    try:
        return yaml.safe_load(РЕЕСТР.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def переключатели() -> list[dict]:
    """Ручки с текущими значениями из конфигов."""
    готово = []
    for строка in реестр().get("переключатели") or []:
        значения = _чит_конфиг(_конфиги().get(строка.get("файл"), ""))
        готово.append(dict(строка, сейчас=значения.get(строка["ключ"])))
    return готово


class Частота:
    """Сколько запросов пришло с адреса за минуту. Забывает само."""

    def __init__(self):
        self.следы: dict[str, deque] = {}

    def можно(self, адрес: str, потолок: int) -> bool:
        сейчас = time.time()
        след = self.следы.setdefault(адрес, deque())
        while след and сейчас - след[0] > 60:
            след.popleft()
        if len(след) >= потолок:
            return False
        след.append(сейчас)
        return True


ЧАСТОТА_ВХОДА = Частота()
ЧАСТОТА_ЧТЕНИЯ = Частота()
# Знак записи на сессию: сам пропуск в тело запроса не кладём.
ЗНАКИ: dict[str, str] = {}


def знак(пропуск: str) -> str:
    return ЗНАКИ.setdefault(пропуск, secrets.token_urlsafe(24))


def значение_не_по_реестру(строка: dict, значение: str) -> str | None:
    """Причина отказа или None. Спрашивать владельца о негодном значении незачем."""
    if строка["вид"] == "выбор":
        можно = [str(з.get("значение")) for з in (строка.get("значения") or [])]
        if значение not in можно:
            return f"значение «{значение}» не из перечня {можно}"
    elif строка["вид"] == "число":
        try:
            число = float(значение)
        except ValueError:
            return f"«{значение}» не число"
        if not (float(строка["от"]) <= число <= float(строка["до"])):
            return f"число вне границ {строка['от']}..{строка['до']}"
    return None


def очередь_дир() -> Path:
    """Где лежит недоставленное. Каталог ОБЩИЙ с демонами, а не свой у панели:
    досылает очередь sentinel, и в PANEL_STATE_DIR он не смотрит (живая
    проверка 11.09.2026 — первое сообщение осталось лежать)."""
    return Path(os.environ.get("LOG_DIR", "/var/log/harness"))


def сказать_владельцу(текст: str) -> None:
    """Сообщение в канал; не ушло — в очередь, её досылает sentinel.

    Запасным кодом входят именно тогда, когда канал лежит. Сообщение о таком
    входе обязано дойти позже, а не пропасть вместе с попыткой.
    """
    отправка = КОРЕНЬ / "scripts" / "tg_send.sh"
    if os.environ.get("PANEL_TG_SEND") == "":
        return
    ушло = False
    if отправка.exists():
        try:
            ушло = subprocess.run(["bash", str(отправка), текст], timeout=30,
                                  capture_output=True).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ушло = False
    if ушло:
        return
    try:
        каталог = очередь_дир()
        каталог.mkdir(parents=True, exist_ok=True)
        with open(каталог / "панель-недоставленное.jsonl", "a",
                  encoding="utf-8") as файл:
            файл.write(json.dumps({"ts": time.time(), "текст": текст},
                                  ensure_ascii=False) + "\n")
    except OSError:
        pass


def записать_в_журнал(событие: dict) -> None:
    событие = dict(событие, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        каталог = состояние_дир()
        каталог.mkdir(parents=True, exist_ok=True)
        with open(каталог / "панель.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(событие, ensure_ascii=False) + "\n")
    except OSError:
        pass


class Панель(BaseHTTPRequestHandler):
    server_version = "harness-panel"

    # ── служебное ───────────────────────────────────────────────────────────
    def log_message(self, формат, *аргументы):   # тише: свой журнал есть
        pass

    @property
    def адрес(self) -> str:
        """Адрес гостя: его ставит НАШ nginx, слушаем только 127.0.0.1 (I12)."""
        return self.headers.get("X-Real-IP") or self.client_address[0]

    @property
    def пропуск(self) -> str | None:
        печенье = self.headers.get("Cookie") or ""
        for кусок in печенье.split(";"):
            имя, _, значение = кусок.strip().partition("=")
            if имя == ИМЯ_ПРОПУСКА:
                return значение
        return None

    def ответить(self, код: int, тело, тип="application/json"):
        данные = (json.dumps(тело, ensure_ascii=False).encode()
                  if тип == "application/json" else тело.encode())
        self.send_response(код)
        self.send_header("Content-Type", f"{тип}; charset=utf-8")
        self.send_header("Content-Length", str(len(данные)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(данные)

    def тело(self) -> dict:
        """Тело запроса: и JSON (наши вызовы), и обычная форма браузера.

        Форма нужна потому, что страница входа обходится БЕЗ скриптов: CSP
        панели запрещает inline-скрипт, и кнопка на fetch просто не работала бы
        (поймано живой попыткой владельца 11.09.2026)."""
        длина = int(self.headers.get("Content-Length") or 0)
        if not длина:
            return {}
        сырое = self.rfile.read(длина).decode("utf-8", "replace")
        if "application/x-www-form-urlencoded" in (self.headers.get("Content-Type") or ""):
            return {к: з[0] for к, з in urllib.parse.parse_qs(сырое).items()}
        try:
            return json.loads(сырое)
        except ValueError:
            return {}

    def свой_источник(self) -> bool:
        """Запрос пришёл с нашей же страницы, а не с чужого сайта (C6).

        Главный рубеж против чужого запроса — знак записи в теле формы: его
        нет ни у кого, кроме нашей страницы. Заголовки источника — ДОПОЛНЕНИЕ,
        и отсутствие их не повод отказывать: браузер в Telegram шлёт «Origin:
        null», и на этом владелец дважды упёрся в «запрос не с этой страницы»
        (11.09.2026 — сперва на входе, потом на шагах пайплайна).
        """
        источник = (self.headers.get("Origin") or "").strip().lower()
        if источник and источник != "null":
            if self.headers.get("Host", "").lower() not in источник:
                записать_в_журнал({"событие": "чужой Origin", "origin": источник,
                                   "адрес": self.адрес})
                return False
        откуда = self.headers.get("Sec-Fetch-Site")
        if откуда == "cross-site" and источник and источник != "null":
            записать_в_журнал({"событие": "запрос с чужого сайта",
                               "origin": источник, "адрес": self.адрес})
            return False
        return True

    # ── маршруты ────────────────────────────────────────────────────────────
    def do_GET(self):
        путь = self.path.split("?")[0].rstrip("/") or "/"
        if not ЧАСТОТА_ЧТЕНИЯ.можно(self.адрес, ЗАПРОСОВ_В_МИНУТУ):
            return self.ответить(429, {"беда": "слишком часто"})
        if путь == "/vhod":
            # Токен НЕ тратится: страницу открывает и предпросмотр Telegram (C4).
            токен = self.path.partition("t=")[2].split("&")[0]
            живой = вход().жив(токен)
            return self.ответить(200, vid.вход_по_ссылке(токен, живой), "text/html")
        if путь == "/zapasnoj":
            return self.ответить(200, vid.запасной_вход(), "text/html")
        if путь == "/sostoyanie":
            if not вход().проверить(self.пропуск):
                return self.ответить(401, {"беда": "нужен пропуск"})
            return self.ответить(200, sostoyanie.снять())
        if путь == "/pamyat":
            if not вход().проверить(self.пропуск):
                return self.ответить(401, {"беда": "нужен пропуск"})
            запрос = urllib.parse.parse_qs(self.path.partition("?")[2])
            имя = (запрос.get("имя") or [""])[0]
            искать = (запрос.get("искать") or [""])[0]
            if имя:
                return self.ответить(200, vid.правка_записи(
                    sostoyanie.запись(имя), знак(self.пропуск)), "text/html")
            return self.ответить(200, vid.память(
                sostoyanie.память(искать), знак(self.пропуск), искать), "text/html")
        if путь == "/karta":
            if not вход().проверить(self.пропуск):
                return self.ответить(401, {"беда": "нужен пропуск"})
            return self.ответить(200, vid.карта_разработки(sostoyanie.karta()),
                                 "text/html")
        if путь == "/ustrojstvo":
            if not вход().проверить(self.пропуск):
                return self.ответить(401, {"беда": "нужен пропуск"})
            запрос = urllib.parse.parse_qs(self.path.partition("?")[2])
            искать = (запрос.get("искать") or [""])[0]
            return self.ответить(200, vid.устройство(
                sostoyanie.устройство(искать), искать), "text/html")
        if путь == "/nastrojki":
            if not вход().проверить(self.пропуск):
                return self.ответить(401, {"беда": "нужен пропуск"})
            вход().продлить(self.пропуск)
            return self.ответить(200, {"переключатели": переключатели(),
                                       "csrf": знак(self.пропуск)})
        if путь == "/":
            if вход().проверить(self.пропуск):
                return self.ответить(200, self._панель(), "text/html")
            return self.ответить(200, vid.гость(), "text/html")
        return self.ответить(404, {"беда": "нет такой страницы"})

    def внутренний(self) -> bool:
        """Запрос пришёл с самой машины, минуя nginx.

        Наш nginx всегда ставит X-Real-IP; снаружи эти маршруты вдобавок
        закрыты в его конфиге. Внутренние вызовы нужны, чтобы состояние панели
        (токены, сессии, ожидания) писала ОДНА сторона — служба: иначе файлы
        пишут два разных пользователя и права молча расходятся (поймано живой
        попыткой входа 11.09.2026).
        """
        if self.headers.get("X-Real-IP") or \
                self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        # Доказательство — ключ, а не адрес: на петле сидят и чужие учётки
        # машины (ревью безопасности 11.09.2026, F-sec-01).
        пришёл = self.headers.get("X-Panel-Key") or ""
        return bool(пришёл) and secrets.compare_digest(пришёл,
                                                       вход().внутренний_ключ())

    def do_POST(self):
        путь = self.path.split("?")[0].rstrip("/") or "/"
        if путь.startswith("/vnutr/"):
            if not self.внутренний():
                return self.ответить(404, {"беда": "нет такой страницы"})
            return self._внутренний_вызов(путь, self.тело())
        if путь == "/vhod":
            # Проверку источника здесь НЕ делаем: единственный секрет этого
            # маршрута — одноразовый токен из чата владельца, а встроенный
            # браузер Telegram шлёт форму без Origin, и владелец упирался в
            # «запрос не с этой страницы» (живая попытка 11.09.2026).
            return self._обмен()
        if путь == "/zapasnoj":
            # Как и обмен токена, этот вход не может требовать заголовка
            # источника: форму шлёт браузер владельца в аварийной ситуации,
            # иногда — встроенный браузер без Origin.
            return self._запасной(self.тело())
        if not self.свой_источник():
            записать_в_журнал({"событие": "чужой источник", "адрес": self.адрес})
            return self.ответить(403, {"беда": "запрос не с этой страницы"})
        if not вход().проверить(self.пропуск):
            return self.ответить(401, {"беда": "нужен пропуск"})
        тело = self.тело()
        if тело.get("csrf") != знак(self.пропуск):
            записать_в_журнал({"событие": "запись без знака", "адрес": self.адрес})
            return self.ответить(403, {"беда": "нет знака записи"})
        if путь in ЗАКРЫТО_ЗАПАСНОЙ and вход().запасная(self.пропуск):
            # Запасным кодом входят, когда канал мёртв, — подтверждать опасное
            # тогда некому. Список путей держится ДАННЫМИ рядом с рубежом:
            # в каждом обработчике порознь он расходится (F-sec-03).
            беда = ("вход сделан запасным кодом: опасные действия закрыты, "
                    "пока канал не отвечает")
            if self._из_браузера():
                return self._вернуть_на_панель(f"Не вышло: {беда}", 403)
            return self.ответить(403, {"беда": беда})
        if путь == "/kljuch":
            return self._переключить(тело)
        if путь == "/shag":
            return self._шаг(тело)
        if путь == "/pamyat":
            return self._сохранить_память(тело)
        if путь == "/nabor":
            return self._набор(тело)
        if путь == "/vyjti":
            вход().отозвать_всё()
            ЗНАКИ.clear()
            записать_в_журнал({"событие": "выход", "адрес": self.адрес})
            if self._из_браузера():
                return self.ответить(200, vid.гость(), "text/html")
            return self.ответить(200, {"готово": True})
        return self.ответить(404, {"беда": "нет такой страницы"})

    def _шаг(self, тело: dict):
        """Включить или выключить шаг работы на уровне задачи."""
        доводы = [str(тело.get("уровень", "")), str(тело.get("шаг", "")),
                  str(тело.get("действие", ""))]
        готово = subprocess.run(
            [sys.executable, str(КОРЕНЬ / "scripts" / "zapisat-shag.py"), *доводы],
            capture_output=True, text=True, timeout=60)
        записать_в_журнал({"событие": "шаг пайплайна", "доводы": доводы,
                           "код": готово.returncode, "адрес": self.адрес})
        if готово.returncode != 0:
            беда = (готово.stderr or готово.stdout).strip()
            if self._из_браузера():
                return self._вернуть_на_панель(f"Не вышло: {беда}", 422)
            return self.ответить(422, {"беда": беда})
        if self._из_браузера():
            слово = "включён" if доводы[2] == "включить" else "выключен"
            return self._вернуть_на_панель(
                f"Шаг «{доводы[1].replace('_', ' ')}» {слово} для уровня "
                f"«{доводы[0]}».")
        return self.ответить(200, {"готово": True})

    def _сохранить_память(self, тело: dict):
        имя, текст = str(тело.get("имя", "")), str(тело.get("текст", ""))
        готово = subprocess.run(
            [sys.executable, str(КОРЕНЬ / "scripts" / "zapisat-pamyat.py"), имя],
            input=текст, capture_output=True, text=True, timeout=60)
        записать_в_журнал({"событие": "правка памяти", "имя": имя,
                           "код": готово.returncode, "адрес": self.адрес})
        if готово.returncode != 0:
            беда = (готово.stderr or готово.stdout).strip()
            return self.ответить(422, vid.правка_записи(
                {"нет данных": f"Не вышло: {беда}"}, знак(self.пропуск)), "text/html")
        return self.ответить(200, vid.память(
            sostoyanie.память(), знак(self.пропуск), "",
            f"Запись «{имя}» сохранена. В хранилище кода она уедет сама."),
            "text/html")

    def _набор(self, тело: dict):
        имя, действие = str(тело.get("имя", "")), str(тело.get("действие", ""))
        готово = subprocess.run(
            [sys.executable, str(КОРЕНЬ / "scripts" / "pamyat-v-paket.py"),
             имя, действие], capture_output=True, text=True, timeout=60)
        записать_в_журнал({"событие": "набор памяти", "имя": имя,
                           "действие": действие, "код": готово.returncode,
                           "адрес": self.адрес})
        сообщение = ((готово.stderr or готово.stdout).strip()
                     if готово.returncode else
                     f"«{имя}»: {действие} в набор новой установки.")
        return self.ответить(200 if not готово.returncode else 422,
                             vid.память(sostoyanie.память(), знак(self.пропуск),
                                        "", сообщение), "text/html")

    def _из_браузера(self) -> bool:
        """Запрос пришёл обычной формой, а не нашим вызовом из кода."""
        return "application/x-www-form-urlencoded" in (
            self.headers.get("Content-Type") or "")

    def _вернуть_на_панель(self, сообщение: str, код: int = 200):
        """Браузеру — снова панель с человеческой строкой о том, что вышло.

        Код ответа честный даже когда страница красивая: 200 на неудачном
        действии прячет отказ от всякой проверки, и однажды уже спрятал
        (включение шага падало кодом 1, а наружу уходило 200)."""
        return self.ответить(код, self._панель(сообщение), "text/html")

    # ── внутренние вызовы (с самой машины) ──────────────────────────────────
    def _внутренний_вызов(self, путь: str, тело: dict):
        chat_id = str(тело.get("chat_id", ""))
        if путь == "/vnutr/token":
            токен = вход().выдать_токен(chat_id)
            if not токен:
                return self.ответить(403, {"беда": "ссылка входа выдаётся "
                                                   "только владельцу"})
            адрес = (_чит_конфиг(_конфиги()["harness.conf"]).get("PANEL_URL")
                     or "").rstrip("/")
            if not адрес:
                return self.ответить(409, {"беда": "панель ещё не выведена "
                                                   "наружу: в harness.conf пуст PANEL_URL"})
            return self.ответить(200, {"ссылка": f"{адрес}/vhod?t={токен}"})
        if путь == "/vnutr/vyjti":
            if chat_id != вход().chat_id:
                return self.ответить(403, {"беда": "выйти может только владелец"})
            вход().отозвать_всё()
            ЗНАКИ.clear()
            return self.ответить(200, {"готово": True})
        if путь == "/vnutr/otvet":
            итог = podtverzhdenie.ответ(str(тело.get("текст", "")))
            return self.ответить(200, {"итог": {True: "применено", False: "отказ",
                                                None: "не ответ панели"}[итог]})
        return self.ответить(404, {"беда": "нет такой страницы"})

    # ── действия ────────────────────────────────────────────────────────────
    def _обмен(self):
        if not ЧАСТОТА_ВХОДА.можно(self.адрес, ПОПЫТОК_ВХОДА_В_МИНУТУ):
            записать_в_журнал({"событие": "частые попытки входа", "адрес": self.адрес})
            return self.ответить(429, {"беда": "слишком часто"})
        пропуск = вход().обменять((self.тело() or {}).get("t", ""), self.адрес)
        if not пропуск:
            записать_в_журнал({"событие": "негодная ссылка", "адрес": self.адрес})
            return self.ответить(403, {"беда": "ссылка не годится"})
        записать_в_журнал({"событие": "вход", "адрес": self.адрес})
        браузер = "application/x-www-form-urlencoded" in (
            self.headers.get("Content-Type") or "")
        self.send_response(303 if браузер else 200)
        if браузер:
            self.send_header("Location", "/")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie",
                         f"{ИМЯ_ПРОПУСКА}={пропуск}; HttpOnly; Secure; SameSite=Strict; "
                         f"Path=/; Max-Age={vhod.СЕССИЯ_ЖИВЁТ_СЕК}")
        данные = json.dumps({"пропуск": пропуск}, ensure_ascii=False).encode()
        self.send_header("Content-Length", str(len(данные)))
        self.end_headers()
        self.wfile.write(данные)

    def _запасной(self, тело: dict):
        """Вход долгим кодом, когда канал молчит: с выдержкой и оглаской."""
        if not ЧАСТОТА_ВХОДА.можно(self.адрес, ПОПЫТОК_ВХОДА_В_МИНУТУ):
            return self.ответить(429, {"беда": "слишком часто"})
        ответ = вход().войти_запасным(str(тело.get("код", "")), self.адрес)
        записать_в_журнал({"событие": "запасной вход", "адрес": self.адрес,
                           "исход": next(iter(ответ))})
        if ответ.get("беда"):
            сказать_владельцу(f"Панель: попытка запасного входа с {self.адрес} — "
                              f"код не подошёл.")
            return self.ответить(403 if not self._из_браузера() else 200,
                                 vid.запасной_вход("Код не подошёл.")
                                 if self._из_браузера() else ответ,
                                 "text/html" if self._из_браузера() else "application/json")
        if ответ.get("ждать"):
            сказать_владельцу(
                f"Панель: кто-то входит запасным кодом с {self.адрес}. "
                f"Вход откроется через {ответ['ждать']} с. Если это не вы — "
                f"напишите боту «панель выйти» и смените код.")
            if self._из_браузера():
                return self.ответить(200, vid.запасной_вход(
                    f"Код принят. Вход откроется через {ответ['ждать']} секунд — "
                    f"введите код ещё раз. Владельцу ушло сообщение о попытке."),
                    "text/html")
            return self.ответить(202, ответ)
        сказать_владельцу(f"Панель: выполнен вход запасным кодом с {self.адрес}. "
                          f"Опасные переключатели этой сессии недоступны.")
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie",
                         f"panel={ответ['пропуск']}; HttpOnly; Secure; "
                         f"SameSite=Lax; Path=/; Max-Age={vhod.СЕССИЯ_ЖИВЁТ_СЕК}")
        self.end_headers()

    def _переключить(self, тело: dict):
        if вход().запасная(self.пропуск):
            # Запасным кодом входят, когда канал мёртв, — а подтверждать
            # опасное тогда некому. Отказ честно называет причину.
            строка = next((с for с in переключатели()
                           if с["ключ"] == str(тело.get("ключ", ""))), None)
            if строка and строка.get("опасный"):
                беда = ("это опасный переключатель, а вход сделан запасным "
                        "кодом: подтвердить его в боте сейчас некому")
                if self._из_браузера():
                    return self._вернуть_на_панель(f"Не вышло: {беда}", 403)
                return self.ответить(403, {"беда": беда})
        ключ, значение = str(тело.get("ключ", "")), str(тело.get("значение", ""))
        строка = next((с for с in переключатели() if с["ключ"] == ключ), None)
        if строка is None:
            записать_в_журнал({"событие": "ключ вне реестра", "ключ": ключ,
                               "адрес": self.адрес})
            return self.ответить(403, {"беда": "этого ключа нет в списке ручек"})
        if строка.get("опасный"):
            # Второй рубеж (C3): панель заводит ожидание и спрашивает владельца
            # в канале одноразовым кодом. Украденный пропуск сам по себе
            # поведение сервера не меняет.
            беда = значение_не_по_реестру(строка, значение)
            if беда:
                return self.ответить(422, {"беда": беда})
            podtverzhdenie.завести(ключ, значение, строка.get("имя", ""))
            записать_в_журнал({"событие": "спрошено подтверждение", "ключ": ключ,
                               "значение": значение, "адрес": self.адрес})
            if self._из_браузера():
                return self._вернуть_на_панель(
                    f"Спросил подтверждение в боте: ответьте «да» с кодом, "
                    f"который пришёл в чат. Пока «{строка['имя']}» не изменён.")
            return self.ответить(409, {"беда": "опасный переключатель требует "
                                               "подтверждения в боте",
                                       "ждём": "ответь в боте: да <код>"})
        готово = subprocess.run([sys.executable, str(ПИСАТЕЛЬ), ключ, значение],
                                capture_output=True, text=True, timeout=30)
        if готово.returncode != 0:
            беда = (готово.stderr or готово.stdout).strip()
            if self._из_браузера():
                return self._вернуть_на_панель(f"Не вышло: {беда}", 422)
            return self.ответить(422, {"беда": беда})
        записать_в_журнал({"событие": "переключено", "ключ": ключ,
                           "значение": значение, "адрес": self.адрес})
        if self._из_браузера():
            return self._вернуть_на_панель(
                f"Готово: «{строка['имя']}» — применяется {строка['применяется']}.")
        return self.ответить(200, {"готово": True,
                                   "применяется": строка.get("применяется")})

    # ── страницы ────────────────────────────────────────────────────────────
    def _панель(self, сообщение: str = "") -> str:
        return vid.панель(sostoyanie.снять(), переключатели(),
                          знак(self.пропуск), сообщение)


def собрать(порт: int = 8787) -> ThreadingHTTPServer:
    # Ключ внутренних вызовов заводится ПРИ СТАРТЕ: если ждать первого вызова,
    # диспетчер получит отказ (ключа ещё нет), а служба его так и не создаст —
    # проверка права выполняется до создания (живая улика 11.09.2026).
    вход().внутренний_ключ()
    return ThreadingHTTPServer(("127.0.0.1", порт), Панель)


def main() -> int:
    порт = 8787
    if "--порт" in sys.argv:
        порт = int(sys.argv[sys.argv.index("--порт") + 1])
    служба = собрать(порт)
    print(f"[панель] слушаю 127.0.0.1:{порт}")
    служба.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
