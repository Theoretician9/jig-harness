#!/usr/bin/env python3
"""Проба ручки HACKATHON_UNTIL: панель и писатель настроек (задача hr.hackathon-switch).

Слово владельца 29.10.2026: «надо простой способ включения выключения
[режима хакатона] в панель и в меню в телеграме». Ручка нового вида «срок»:
кнопка «включить на 72 часа» пишет в harness.conf момент конца режима,
«выключен» — пустую строку. Судья включённости один — scripts/hackathon.py
active(); проба проверяет, что записанное писателем он понимает.

Всё пишется во временный каталог (свои harness.conf, install.conf, журнал,
PANEL_NO_SUDO=1): боевой /etc/harness проба не трогает. Строка реестра
судится по НАСТОЯЩЕМУ switches.yaml — тому, что увидит владелец; писатель,
вид и сверка реестра — по строке из задачи, чтобы их отказ назывался своей
причиной, а не отсутствием строки.

Прогон:
    python3 scripts/test_hackathon_switch.py
Код возврата: 0 — все случаи прошли, 1 — есть упавший.
"""
import datetime as dt
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "harness" / "panel"
sys.path.insert(0, str(PANEL))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import yaml  # noqa: E402
import config  # noqa: E402

KEY = "HACKATHON_UNTIL"
WRITER = ROOT / "scripts" / "write-key.py"
# Строка из задачи: по ней судятся писатель, вид и сверка реестра.
EXPECTED_ROW = {
    "ключ": KEY, "файл": "harness.conf", "имя": "Режим хакатона", "вид": "срок",
    "значения": [{"значение": "0", "подпись": "выключен", "по_умолчанию": True},
                 {"значение": "72", "подпись": "включить на 72 часа"}],
    "простыми словами": "облегчённый пайплайн на 72 часа",
    "опасный": False, "читатель": "scripts/hackathon.py",
    "способ_чтения": "при_вызове", "применяется": "сразу",
}
ISO_LINE = re.compile(rf'^{KEY}="([^"\s]+)"$', re.MULTILINE)
cases = 0
failed = 0


def verdict(ok: bool, text: str) -> None:
    global cases, failed
    cases += 1
    if not ok:
        failed += 1
    print(("ок  · " if ok else "ПЛОХО · ") + text)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def key_line(path: Path) -> str | None:
    """Строка ключа в конфиге как есть (с кавычками) или None."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{KEY}="):
            return line
    return None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(url, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as answer:
            return answer.status, answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def clock_marks(moment: dt.datetime) -> set:
    """Как «срок» может быть напечатан: часы:минуты в поясе записи или в UTC."""
    return {f"{moment:%H:%M}", f"{moment.astimezone(dt.timezone.utc):%H:%M}"}


hackathon = load("hackathon_probe", ROOT / "scripts" / "hackathon.py")

# ── 1. строка реестра — та, что увидит владелец ─────────────────────────────
registry = yaml.safe_load((ROOT / "harness/config/switches.yaml").read_text(encoding="utf-8"))
row = next((r for r in registry.get("переключатели") or [] if r.get("ключ") == KEY), None)
verdict(row is not None, f"ручка {KEY} есть в реестре switches.yaml")
row = row or {}
verdict(row.get("файл") == "harness.conf" and row.get("вид") == "срок",
        f"ручка живёт в harness.conf и это вид «срок»: {row.get('файл')} / {row.get('вид')}")
values = {str(v.get("значение")): v for v in row.get("значения") or []}
verdict(set(values) == {"0", "72"} and values["0"].get("по_умолчанию") is True
        and values["0"].get("подпись") == "выключен"
        and values["72"].get("подпись") == "включить на 72 часа",
        f"значения 0 «выключен» (умолчание) и 72 «включить на 72 часа»: {sorted(values)}")
verdict(row.get("опасный") is False,
        "ручка не опасная: подтверждения кодом в боте не требует")
verdict(row.get("читатель") == "scripts/hackathon.py"
        and row.get("способ_чтения") == "при_вызове" and row.get("применяется") == "сразу",
        "читатель hackathon.py, читается при вызове, применяется сразу: "
        f"{row.get('читатель')} / {row.get('способ_чтения')} / {row.get('применяется')}")

# ── 2. умолчание: режим выключен и на новой машине ──────────────────────────
for label, sample in (("проекта", ROOT / "harness/config/harness.conf"),
                      ("пакета", config.katalog_paketa() / "harness/config/harness.conf")):
    line = key_line(sample) if sample.exists() else None
    verdict(line == f'{KEY}=""',
            f"образец harness.conf {label} несёт {KEY}=\"\" (режим выключен): {line!r}")

# ── 3. писатель: вид «срок» пишет момент, «0» — пусто ───────────────────────
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    conf = tmp / "harness.conf"
    conf.write_text(f'CTX_ROTATE_PCT=75\n{KEY}=""\n', encoding="utf-8")
    (tmp / "install.conf").write_text('TG_CHAT_ID="100000001"\n', encoding="utf-8")
    (tmp / "log").mkdir()
    reg = tmp / "switches.yaml"
    reg.write_text(yaml.safe_dump({"переключатели": [EXPECTED_ROW]}, allow_unicode=True),
                   encoding="utf-8")
    env = dict(os.environ, HARNESS_CONF=str(conf), HARNESS_INSTALL_CONF=str(tmp / "install.conf"),
               LOG_DIR=str(tmp / "log"), HARNESS_PERECLUCHATELI=str(reg), PANEL_NO_SUDO="1")

    def write(value: str):
        return subprocess.run([sys.executable, str(WRITER), KEY, value], env=env,
                              capture_output=True, text=True, timeout=60)

    before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    done = write("72")
    after = dt.datetime.now(dt.timezone.utc)
    verdict(done.returncode == 0,
            f"write-key {KEY} 72 → код 0: {done.returncode} {done.stderr.strip()[:160]}")
    found = ISO_LINE.search(conf.read_text(encoding="utf-8"))
    verdict(found is not None,
            f"в конфиге строка {KEY}=\"<момент>\" в кавычках: {key_line(conf)!r}")
    moment = None
    if found:
        try:
            moment = dt.datetime.fromisoformat(found.group(1))
        except ValueError:
            moment = None
    verdict(moment is not None and moment.tzinfo is not None and moment.microsecond == 0
            and "." not in found.group(1),
            f"момент — ISO 8601 с поясом, без микросекунд: {found and found.group(1)!r}")
    verdict(moment is not None and moment.tzinfo is not None
            and before + dt.timedelta(hours=72) <= moment
            <= after + dt.timedelta(hours=72, seconds=1),
            f"момент = сейчас + 72 часа: {moment}")
    verdict(hackathon.active(conf_path=conf) is True,
            f"hackathon.active() после записи 72 → True: {hackathon.active(conf_path=conf)}")
    verdict(moment is not None and hackathon.until(conf_path=conf) == moment,
            f"hackathon.until() разбирает записанное: {hackathon.until(conf_path=conf)}")
    verdict("CTX_ROTATE_PCT=75" in conf.read_text(encoding="utf-8"),
            "соседний ключ конфига цел")
    copies = list((tmp / "log" / "панель-копии").glob("harness.conf.*"))
    verdict(len(copies) >= 1, f"копия прежнего конфига снята (И-1): {len(copies)}")
    journal = tmp / "log" / "панель.jsonl"
    entries = [json.loads(s) for s in journal.read_text(encoding="utf-8").splitlines()
               if s.strip()] if journal.exists() else []
    verdict(any(e.get("ключ") == KEY for e in entries),
            f"запись в журнале панели о смене {KEY}: записей {len(entries)}")

    done = write("0")
    verdict(done.returncode == 0 and key_line(conf) == f'{KEY}=""',
            f"write-key {KEY} 0 → код 0 и строка {KEY}=\"\": {done.returncode} {key_line(conf)!r}")
    verdict(hackathon.active(conf_path=conf) is False and hackathon.until(conf_path=conf) is None,
            f"после «0» режим выключен: active={hackathon.active(conf_path=conf)}")

    kept = conf.read_text(encoding="utf-8")
    for bad in ("48", "72h", "1;id", "-72", ""):
        done = write(bad)
        verdict(done.returncode == 3 and conf.read_text(encoding="utf-8") == kept,
                f"негодное «{bad}» → код 3, файл цел: {done.returncode}")

# ── 3б. hackathon.py --выключить: пустой срок без панели и бота ─────────────
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    conf = tmp / "harness.conf"
    conf.write_text(f'CTX_ROTATE_PCT=75\n{KEY}=""\n', encoding="utf-8")
    (tmp / "install.conf").write_text('TG_CHAT_ID="100000001"\n', encoding="utf-8")
    (tmp / "log").mkdir()
    reg = tmp / "switches.yaml"
    reg.write_text(yaml.safe_dump({"переключатели": [EXPECTED_ROW]}, allow_unicode=True),
                   encoding="utf-8")
    env = dict(os.environ, HARNESS_CONF=str(conf), HARNESS_INSTALL_CONF=str(tmp / "install.conf"),
               LOG_DIR=str(tmp / "log"), HARNESS_PERECLUCHATELI=str(reg), PANEL_NO_SUDO="1")
    done = subprocess.run([sys.executable, str(WRITER), KEY, "72"], env=env,
                          capture_output=True, text=True, timeout=60)
    verdict(done.returncode == 0 and hackathon.active(conf_path=conf) is True,
            f"условие создано: срок записан, active=True: {done.returncode} {key_line(conf)!r}")
    for attempt, before in (("при записанном сроке", None), ("при уже пустом", "0")):
        if before is not None:
            subprocess.run([sys.executable, str(WRITER), KEY, before], env=env,
                           capture_output=True, text=True, timeout=60)
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "hackathon.py"),
                               "--выключить"], env=env, capture_output=True, text=True,
                              timeout=60)
        verdict(done.returncode == 0 and key_line(conf) == f'{KEY}=""'
                and hackathon.active(conf_path=conf) is False,
                f"hackathon.py --выключить {attempt} → код 0, строка {KEY}=\"\", active=False: "
                f"{done.returncode} {key_line(conf)!r} {done.stdout.strip()[:80]!r}")
    verdict("CTX_ROTATE_PCT=75" in conf.read_text(encoding="utf-8"),
            "hackathon.py --выключить: соседний ключ конфига цел")

# ── 4. сверка реестра принимает вид «срок» ─────────────────────────────────
switches = load("check_switches_probe", ROOT / "scripts" / "check-switches.py")
future = (dt.datetime.now(dt.timezone.utc).astimezone()
          + dt.timedelta(hours=72)).replace(microsecond=0)
past = (dt.datetime.now(dt.timezone.utc).astimezone()
        - dt.timedelta(hours=1)).replace(microsecond=0)
for value, label in (("", "пусто"), (future.isoformat(), "момент с поясом")):
    trouble = switches.проверить_значение(EXPECTED_ROW, value)
    verdict(trouble == [], f"check-switches: вид «срок», в конфиге {label} — чисто: {trouble}")
trouble = switches.проверить_значение(EXPECTED_ROW, "мусор")
verdict(bool(trouble) and not any("неизвестный вид" in t for t in trouble),
        f"check-switches: в конфиге не момент — беда, и не «неизвестный вид»: {trouble}")
trouble = switches.проверить_значение(EXPECTED_ROW, future.replace(tzinfo=None).isoformat())
verdict(any("пояс" in t for t in trouble),
        f"check-switches: момент без пояса — беда о поясе: {trouble}")
trouble = switches.проверить_значение(dict(EXPECTED_ROW, значения=[]), "")
verdict(any("пуст" in t for t in trouble),
        f"check-switches: вид «срок» с пустым перечнем — беда: {trouble}")
with tempfile.TemporaryDirectory() as tmp:
    sample = Path(tmp) / "harness.conf"
    sample.write_text(f'{KEY}=""\n', encoding="utf-8")
    trouble = switches.проверить({"переключатели": [EXPECTED_ROW]}, {"harness.conf": str(sample)})
verdict(trouble == [], f"check-switches: полная сверка строки «срок» — чисто: {trouble}")

# ── 5. вид ручки: состояние словами и две кнопки ───────────────────────────
import vid  # noqa: E402

for value, must, must_not, label in (
        ("", "выключен", ("включён до", "срок истёк"), "пусто"),
        (future.isoformat(), "включён до", ("срок истёк",), "срок впереди"),
        (past.isoformat(), "срок истёк", ("включён до",), "срок прошёл"),
        # Судья hackathon.until() момент без пояса отвергает — вид не вправе
        # показывать режим включённым.
        (future.replace(tzinfo=None).isoformat(), "выключен", ("включён до", "срок истёк"),
         "срок без пояса")):
    try:
        page = vid._ручка(dict(EXPECTED_ROW, сейчас=value, состояние="значение"), "csrf-probe")
    except Exception as error:   # вид, не знающий «срок», вправе упасть — это и есть красный
        page = f"ОШИБКА ВИДА: {error!r}"
    # Состояние судится ВНЕ кнопок: подпись кнопки «выключен» не должна
    # засчитываться как слово состояния.
    outside = re.sub(r"<form.*?</form>", "", page, flags=re.S)
    shown = must in outside and not any(m in outside for m in must_not)
    if must == "включён до":
        shown = shown and any(mark in outside for mark in clock_marks(future))
    verdict(shown, f"вид ручки при «{label}» говорит «{must}» вне кнопок: {outside[:200]!r}")
    buttons = re.findall(r'name="значение" value="([^"]*)"[^>]*>\s*<button[^>]*>([^<]*)</button>',
                         page)
    verdict(sorted(buttons) == [("0", "выключен"), ("72", "включить на 72 часа")]
            and 'type="number"' not in page,
            f"вид ручки при «{label}»: кнопки «выключен» и «включить на 72 часа», "
            f"без поля числа: {buttons}")

# Истёкший момент в конфиге надо уметь стереть: «выключен» недоступна только при пусто.
for value, locked, label in (("", True, "отключена при пусто"),
                             (past.isoformat(), False, "доступна при истёкшем сроке")):
    page = vid._ручка(dict(EXPECTED_ROW, сейчас=value, состояние="значение"), "csrf-probe")
    off = re.search(r'name="значение" value="0"[^>]*>\s*<button([^>]*)>', page)
    verdict(off is not None and ("disabled" in off.group(1)) is locked,
            f"кнопка «выключен» {label}: {off and off.group(1)!r}")

# ── 6. живая служба: ручка видна и пишется без подтверждения ───────────────
os.environ.pop("HARNESS_PERECLUCHATELI", None)
try:
    import server
except ImportError as error:
    verdict(False, f"службы панели нет: {error}")
    server = None

if server is not None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        conf = tmp / "harness.conf"
        conf.write_text(f'CTX_ROTATE_PCT=75\n{KEY}=""\n', encoding="utf-8")
        (tmp / "install.conf").write_text('TG_CHAT_ID="100000001"\n', encoding="utf-8")
        for name in ("log", "secrets", "state"):
            (tmp / name).mkdir()
        os.environ.update(HARNESS_CONF=str(conf), HARNESS_INSTALL_CONF=str(tmp / "install.conf"),
                          LOG_DIR=str(tmp / "log"), SECRETS_DIR=str(tmp / "secrets"),
                          PANEL_STATE_DIR=str(tmp / "state"), PANEL_NO_SUDO="1")

        port = free_port()
        server.Панель.log_message = lambda self, f, *a: sys.stderr.write((f % a) + "\n")
        service = server.собрать(порт=port)
        threading.Thread(target=service.serve_forever, daemon=True).start()
        time.sleep(0.3)
        base = f"http://127.0.0.1:{port}"
        own = {"Origin": base, "Sec-Fetch-Site": "same-origin"}
        token = server.вход().выдать_токен("100000001")
        code, body = request(f"{base}/vhod", data={"t": token}, headers=own)
        session_pass = json.loads(body).get("пропуск") if code == 200 else None
        verdict(bool(session_pass), f"условие создано: вход дал пропуск ({code})")
        with_pass = dict(own, Cookie=f"panel={session_pass}")

        code, body = request(f"{base}/nastrojki", headers=with_pass)
        settings = json.loads(body) if code == 200 else {}
        knob = next((s for s in settings.get("переключатели", []) if s.get("ключ") == KEY), None)
        verdict(knob is not None and knob.get("сейчас") == "" and knob.get("состояние") == "значение",
                f"/nastrojki отдаёт ручку {KEY} с пустым значением: {knob and knob.get('сейчас')!r}")
        csrf = settings.get("csrf")

        code, body = request(f"{base}/", headers=with_pass)
        verdict(code == 200 and f'name="ключ" value="{KEY}"' in body
                and "включить на 72 часа" in body,
                f"главная страница рисует ручку и кнопку «включить на 72 часа» ({code})")

        code, body = request(f"{base}/kljuch", headers=with_pass,
                             data={"ключ": KEY, "значение": "72", "csrf": csrf})
        verdict(code == 200 and hackathon.active(conf_path=conf) is True,
                f"кнопка 72 пишет сразу, без кода в боте: ответ {code}, "
                f"active={hackathon.active(conf_path=conf)}, строка {key_line(conf)!r}")
        code, body = request(f"{base}/kljuch", headers=with_pass,
                             data={"ключ": KEY, "значение": "0", "csrf": csrf})
        verdict(code == 200 and key_line(conf) == f'{KEY}=""'
                and hackathon.active(conf_path=conf) is False,
                f"кнопка «выключен» пишет пусто: ответ {code}, строка {key_line(conf)!r}")
        kept = conf.read_text(encoding="utf-8")
        code, body = request(f"{base}/kljuch", headers=with_pass,
                             data={"ключ": KEY, "значение": "48", "csrf": csrf})
        verdict(code == 422 and conf.read_text(encoding="utf-8") == kept,
                f"негодное «48» — отказ 422, файл цел: {code}")
        service.shutdown()

print(f"РУЧКА-ХАКАТОН: случаев {cases}, неудач {failed}")
sys.exit(1 if failed else 0)
