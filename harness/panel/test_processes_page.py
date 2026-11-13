#!/usr/bin/env python3
"""Проба раздела панели «Процессы» (/processy): библиотека, расходы, служба проверок.

Спека hr.process-dispatcher §Е.4. Каждый путь назван поломкой, на которой он
краснеет. Служба поднимается на свободном порту во временном каталоге (образец
test_server.py); библиотека, витрина, журнал запусков, срез и systemctl —
свои подделки через двери окружения. Живые /var/log/harness и живая панель не
трогаются.

Прогон (только фоном):
    python3 harness/panel/test_processes_page.py
"""
import collections
import html
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

paths = 0
failures = 0


def ok(text):
    global paths
    paths += 1
    print("ок  · " + text)


def bad(text):
    global paths, failures
    paths += 1
    failures += 1
    print("ПЛОХО · " + text)


def check(condition, text, why=""):
    if condition:
        ok(text)
    else:
        bad(f"{text} — {why}" if why else text)


def finish():
    print(f"СТРАНИЦА ПРОЦЕССОВ: путей {paths}, неудач {failures}")
    sys.exit(1 if failures else 0)


def find_up(relative):
    # Раскладка проекта и пакета разная: ищем вверх от каталога панели.
    for folder in [HERE, *HERE.parents]:
        if (folder / relative).is_file():
            return folder / relative
    return None


SOURCE_JSON = find_up("config/process-library.json") or find_up("harness/config/process-library.json")
SOURCE_YAML = find_up("config/process-library.yaml") or find_up("harness/config/process-library.yaml")
LIB_MODULE = find_up("scripts/lib/process_library.py")
DISPATCHER = find_up("scripts/process-dispatcher.py")
STATE_MODULE = find_up("scripts/lib/process_state.py")
DEMON_SCRIPT = find_up("scripts/claude-demon.sh")
SOURCE_MECHANISMS = find_up("config/mechanisms.yaml") or find_up("harness/config/mechanisms.yaml")
PLAYWRIGHT_PYTHON = os.environ.get("PANEL_PLAYWRIGHT_PYTHON") or "/home/user/.local/share/playwright-venv/bin/python"
# 20 видимых строк таблицы расходов (спека hr.zapuskov §4.2) — карточки на телефоне: прежние 3 500 недостижимы.
# Наибольший замер стенда на живых данных — 6 306 px; порог 6 400 + 10 % запаса = 7 040, решение
# управляющего по §5 п.8. Выше — снова лента, а не рост числа процессов.
PHONE_MAX_HEIGHT = 7040
PHONE_MAX_ROW = 260         # строка «Все процессы» на телефоне: пары «подпись | значение» в одну строку
BAR_MAX_LINES = 10
if not (SOURCE_JSON and SOURCE_YAML and LIB_MODULE):
    bad(f"не найдены исходники стенда: json={SOURCE_JSON} yaml={SOURCE_YAML} lib={LIB_MODULE}")
    finish()

import yaml  # noqa: E402

SECRET = "SEKRET123"  # не-секрет: маркер утечки для пробы
TOKEN_FLAG = "--token"  # не-секрет: подставной флаг утечки для пробы
ROLE_MARKER = "РОЛЬ-МАРКЕР проба механизма"
EVIL_TITLE = "<script>alert(1)</script> Подделка названия"


# ── своя библиотека ─────────────────────────────────────────────────────────
def build_library(folder: Path):
    """Копия живой библиотеки, НАРОЧНО отличная от неё: счётчики классов и
    число исключений сдвинуты, чтобы страница с вписанными числами 36/34/3/12
    краснела, а не совпадала с живыми данными случайно."""
    lib = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    sched = yaml.safe_load(SOURCE_YAML.read_text(encoding="utf-8"))
    exemptions = sched["выключатель"]["исключения"]
    dropped_exemption = exemptions.pop()  # запись остаётся фоновой, но уже без исключения
    exempt_ids = {e["id"] for e in exemptions}
    records = lib["процессы"]
    keep_ids = exempt_ids | {dropped_exemption["id"]}
    drop = []
    quota = {"фоновый": 5, "по внутреннему действию": 1, "сторонний": 1}
    for record in records:
        cls = record["класс"]
        if (quota.get(cls, 0) > 0 and record["id"] not in keep_ids
                and record["запуск"] not in ("служба-проверок", "хук", "служба")
                and not record["статус"].startswith("снят")):
            quota[cls] -= 1
            drop.append(record["id"])
    records[:] = [r for r in records if r["id"] not in drop]

    # Запись без «зачем» с путём механизма: «что делает» берётся из роли механизма.
    by_mechanism = next(r for r in records if not r.get("зачем") and r["класс"] == "фоновый"
                        and r["id"] not in exempt_ids and r["запуск"] == "крон")
    # Утечка: командная строка с секретом в записи JSON и в YAML.
    leaky = next(r for r in records if r["запуск"] == "крон" and r["id"] != by_mechanism["id"])
    leaky["команда"] = f"harness/demons/x.sh {TOKEN_FLAG}={SECRET}"
    sched["процессы"].setdefault(leaky["id"], {})["команда"] = f"x.sh {TOKEN_FLAG}={SECRET}"
    # Экранирование: название с тегом выводится текстом.
    evil = next(r for r in records if r["класс"] == "по внутреннему действию")
    evil["название"] = EVIL_TITLE

    (folder / "process-library.json").write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
    (folder / "process-library.yaml").write_text(yaml.safe_dump(sched, allow_unicode=True), encoding="utf-8")
    mechanisms = {"механизмы": [{
        "файл": by_mechanism["файлы"][0], "вид": "демон", "тема": "проба", "имя": "Механизм пробы",
        "роль": ROLE_MARKER + "\n", "когда": "по расписанию", "без_него": "проба краснеет\n"}]}
    (folder / "mechanisms.yaml").write_text(yaml.safe_dump(mechanisms, allow_unicode=True), encoding="utf-8")
    return lib, sched, by_mechanism, evil


# ── своя витрина, журнал запусков, срез, systemctl ──────────────────────────
NOW = datetime.now().astimezone()


def iso(moment):
    return moment.isoformat(timespec="seconds")


def view_row(pid, **fields):
    row = {"id": pid, "ядро_ч": 0.5, "доля_ёмкости_проц": 5.0, "память_пик_мб": 200, "память_ср_мб": 100,
           "запусков": 10, "в_работе_ч": 1.0, "доля_запусков_проц": 4.0, "пропусков": 0, "токены": 0,
           "доля_токенов_проц": 0.0, "точность": {"сервер": "точно", "запуски": "точно", "токены": "точно"}}
    row.update(fields)
    return row


def build_view(taken, ids):
    """ids: (строка с оценкой токенов, строка с null, строка окна 7д)."""
    approx_id, null_id, week_id = ids
    day = [view_row(approx_id, ядро_ч=0.9, доля_ёмкости_проц=22.0, токены=1200000, доля_токенов_проц=11.0,
                    точность={"сервер": "точно", "запуски": "точно", "токены": "≈"},
                    cmdline=f"python3 x.py {TOKEN_FLAG}={SECRET}"),
           view_row(null_id, ядро_ч=None, память_пик_мб=None, доля_ёмкости_проц=None),
           view_row("claude-session", ядро_ч=1.2, токены=5000000, доля_токенов_проц=80.0)]
    return {"снято": iso(taken), "выключатель": {"ключ": "off", "исключений": 11},
            "окна": {"сейчас": {"покрытие_ч": None, "строки": []},
                     "24ч": {"покрытие_ч": 21.5, "строки": day},
                     "7д": {"покрытие_ч": 100.0, "строки": [view_row(week_id)]}},
            "сверка": {"расхождение_токенов": 0, "без_метки_проц": 0.0},
            "неразмечено": {"cron.service": 3.2, "вне_корзин": 1.1}}


# Поддельный systemctl: list-timers — JSON из timers.json, list-units (--state=failed) —
# JSON из failed.json, show — key=value из show.json блоками через пустую строку, только
# запрошенные -p; юнит не из таблицы берёт запись «*», без неё — not-found. Есть fail.txt —
# любой вызов: его текст в stderr и код 1.
FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
if (here / "fail.txt").exists():
    sys.stderr.write((here / "fail.txt").read_text())
    sys.exit(1)
if "list-units" in args:
    failed = here / "failed.json"
    print(failed.read_text() if failed.exists() else "[]")
    sys.exit(0)
if "list-timers" in args:
    print((here / "timers.json").read_text())
    sys.exit(0)
if args and args[0] == "show":
    table = json.loads((here / "show.json").read_text())
    props, units, i = [], [], 1
    while i < len(args):
        a = args[i]
        if a in ("-p", "--property"):
            props += args[i + 1].split(","); i += 2; continue
        if a.startswith("--property="):
            props += a.split("=", 1)[1].split(","); i += 1; continue
        if a.startswith("-p") and len(a) > 2:
            props += a[2:].split(","); i += 1; continue
        if not a.startswith("-"):
            units.append(a)
        i += 1
    blocks = []
    for unit in units:
        known = table.get(unit, table.get("*", {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": ""}))
        names = props or list(known)
        blocks.append("\n".join(f"{p}={known.get(p, '')}" for p in names))
    print("\n\n".join(blocks))
    sys.exit(0)
sys.exit(1)
'''


def write_checks_fixtures(folder: Path, cgroup: Path, check_ids, installed=True, memory_max="2684354560"):
    def usec(moment):
        return int(moment.timestamp() * 1_000_000)

    timers, show = [], {}
    for pid in check_ids:
        timer, service = f"harness-{pid}.timer", f"harness-{pid}.service"
        timers.append({"next": usec(NOW + timedelta(minutes=14)), "left": usec(NOW + timedelta(minutes=14)),
                       "last": usec(NOW - timedelta(minutes=6)), "passed": 360_000_000,
                       "unit": timer, "activates": service})
        show[timer] = {"Id": timer, "ActiveState": "active", "UnitFileState": "enabled", "LoadState": "loaded"}
        show[service] = {"Id": service, "ActiveState": "inactive", "UnitFileState": "static", "LoadState": "loaded",
                         "ExecMainStartTimestamp": "", "Result": "success"}
    running, disabled = check_ids[0], check_ids[-1]
    # Идёт сейчас: у systemctl NEXT пуст, служба active.
    timers[0]["next"] = timers[0]["left"] = None
    show[f"harness-{running}.service"].update(
        ActiveState="active", ExecMainStartTimestamp=(NOW - timedelta(minutes=3)).strftime("%a %Y-%m-%d %H:%M:%S %z"))
    # Таймер не включён.
    timers[-1]["next"] = timers[-1]["left"] = None
    show[f"harness-{disabled}.timer"].update(ActiveState="inactive", UnitFileState="disabled")
    show["harness-checks.slice"] = {"Id": "harness-checks.slice", "ActiveState": "active", "LoadState": "loaded",
                                    "CPUQuotaPerSecUSec": "2s", "MemoryHigh": "2147483648",
                                    "MemoryMax": "infinity" if memory_max == "max" else memory_max,
                                    "MemoryCurrent": "1675640832"}
    # Юниты записей библиотеки, которых нет в таблице, — живые: на стенде выше «ещё N» не больше
    # горстки не-зелёных строк (§5 п.8), упавшие задаёт сам случай.
    show["*"] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success",
                 "ExecMainStatus": "0", "UnitFileState": "enabled"}
    if not installed:
        timers, show = [], {}
    (folder / "timers.json").write_text(json.dumps(timers), encoding="utf-8")
    (folder / "show.json").write_text(json.dumps(show), encoding="utf-8")
    cgroup.mkdir(exist_ok=True)
    files = {"cpu.max": "200000 100000\n",
             "cpu.stat": "usage_usec 9000000\nuser_usec 7000000\nsystem_usec 2000000\n"
                         "nr_periods 500\nnr_throttled 7\nthrottled_usec 120000\n",
             "memory.current": "1675640832\n", "memory.high": "2147483648\n",
             "memory.max": memory_max + "\n", "memory.peak": "2000000000\n"}
    for name, text in files.items():
        (cgroup / name).write_text(text, encoding="utf-8")
    return running, disabled


def write_runs(logs: Path, check_ids, lines=True):
    path = logs / "process-runs.jsonl"
    if not lines:
        path.write_text("", encoding="utf-8")
        return
    start = NOW - timedelta(minutes=30)
    events = []
    for pid, rc in ((check_ids[1], 0), (check_ids[2], 3)):
        events.append({"v": 1, "ts": iso(start), "src": "scripts/run-background.sh", "event": "process.start",
                       "outcome": "ok", "data": {"id": pid, "pid": 111, "sid": 111}})
        events.append({"v": 1, "ts": iso(start + timedelta(seconds=40)), "src": "scripts/run-background.sh",
                       "event": "process.end", "outcome": "ok" if rc == 0 else "fail",
                       "data": {"id": pid, "pid": 111, "rc": rc, "с": 40.0, "cpu_с": 12.5}})
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")


# ── разбор HTML ─────────────────────────────────────────────────────────────
def plain(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text).replace("\xa0", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", text)


def blocks(page: str) -> dict:
    """Три блока по якорям в порядке спеки: lib → cost → checks."""
    marks = {name: page.find(f'id="{name}"') for name in ("lib", "cost", "checks")}
    if any(pos < 0 for pos in marks.values()) or not marks["lib"] < marks["cost"] < marks["checks"]:
        return {}
    return {"lib": page[marks["lib"]:marks["cost"]], "cost": page[marks["cost"]:marks["checks"]],
            "checks": page[marks["checks"]:]}


def cards(lib_block: str) -> dict:
    """id → [html карточки]. Карточка начинается с data-id="<id>"."""
    found = list(re.finditer(r'data-id="([^"]+)"', lib_block))
    result = {}
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(lib_block)
        result.setdefault(html.unescape(match.group(1)), []).append(lib_block[match.start():end])
    return result


def card_title(card: str):
    match = re.search(r"<h3[^>]*>(.*?)</h3>", card, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else None


def card_text(cards_map, pid):
    return plain("".join(cards_map.get(pid, [])))


def links(fragment: str) -> list:
    """[(параметры адреса, текст ссылки)]."""
    result = []
    for match in re.finditer(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', fragment, re.S):
        query = urllib.parse.urlsplit(html.unescape(match.group(1))).query
        result.append(({k: v[0] for k, v in urllib.parse.parse_qs(query).items()}, plain(match.group(2))))
    return result


def has_number(text, number):
    return re.search(rf"(?<!\d){number}(?!\d)", text) is not None


def table_rows(fragment: str) -> list:
    """[[(data-label, текст ячейки, сырой html ячейки)]] по строкам таблицы."""
    rows = []
    for row in re.findall(r"<tr\b.*?</tr>", fragment, re.S):
        cells = re.findall(r"<td\b([^>]*)>(.*?)</td>", row, re.S)
        if cells:
            rows.append([(html.unescape((re.search(r'data-label="([^"]*)"', attrs) or [None, ""])[1]),
                          plain(body), body) for attrs, body in cells])
    return rows


def cell(row, label_start):
    for label, text, raw in row:
        if label.casefold().startswith(label_start.casefold()):
            return text, raw
    return None, None


def window_cpu(row):
    """Ячейка «ЦП <окно>» — второй столбец ЦП, не «ЦП 10 мин»."""
    for label, text, raw in row:
        if label.casefold().startswith("цп") and "10 мин" not in label:
            return text, raw
    return None, None


def state_values() -> list:
    """Значения состояния — разбором константы VALUES в process_state.py (пустой список — красный)."""
    import ast
    try:
        tree = ast.parse(STATE_MODULE.read_text(encoding="utf-8")) if STATE_MODULE else None
    except (OSError, SyntaxError):
        tree = None
    for node in tree.body if tree else []:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VALUES" for t in node.targets) \
                and isinstance(node.value, (ast.Tuple, ast.List)):
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    return []


def more_details(cost: str):
    """(видимая часть, скрытая под «ещё N», текст summary) таблицы расходов."""
    for begin, end, _, inner in top_details(cost):
        summary = summary_text(inner).strip()
        if summary.startswith("ещё"):
            return cost[:begin] + cost[end:], inner, summary
    return cost, "", ""


def media_body(css, match):
    depth, end = 1, match.end()
    while end < len(css) and depth:
        depth += {"{": 1, "}": -1}.get(css[end], 0)
        end += 1
    return css[match.end():end - 1], end


def outside_min_width_media(css):
    """CSS без блоков @media (min-width…) — там широкое законно."""
    out, pos = [], 0
    for match in re.finditer(r"@media[^{]*\{", css):
        if match.start() < pos:
            continue
        inner, end = media_body(css, match)
        out.append(css[pos:match.start()])
        if "min-width" not in match.group(0):
            out.append(inner)
        pos = end
    out.append(css[pos:])
    return "".join(out)


def top_details(fragment: str) -> list:
    """[(начало, конец, открывающий тег, html внутри)] верхних <details> с учётом вложенных."""
    found, depth, start = [], 0, None
    for match in re.finditer(r"<(/?)details\b[^>]*>", fragment):
        if not match.group(1):
            start = match if depth == 0 else start
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                found.append((start.start(), match.end(), start.group(0), fragment[start.end():match.start()]))
    return found


def div_end(fragment: str, begin: int) -> int:
    """Конец элемента <div>, открытого в begin: по счёту вложенных <div> и </div>."""
    depth = 0
    for tag in re.finditer(r"<(/?)div\b", fragment[begin:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return begin + tag.end()
    return len(fragment)


def without_details(fragment: str) -> str:
    """Что видно без нажатий: фрагмент без верхних <details>."""
    out, pos = [], 0
    for begin, end, _, _ in top_details(fragment):
        out.append(fragment[pos:begin])
        pos = end
    return "".join(out + [fragment[pos:]])


def is_open(tag: str) -> bool:
    return re.search(r"\sopen(?:[\s=>]|$)", tag) is not None


def summary_text(inner: str) -> str:
    match = re.search(r"<summary\b[^>]*>(.*?)</summary>", inner, re.S)
    return plain(match.group(1)) if match else ""


def library_groups(lib_block: str) -> list:
    """Группы библиотеки: верхние <details>, внутри которых есть карточки data-id.
    [(открывающий тег, текст summary, {id карточки → [html]})]."""
    return [(tag, summary_text(inner), cards(inner))
            for _, _, tag, inner in top_details(lib_block) if "data-id=" in inner]


def distinct_names(pairs, banned=()):
    """[(id, название)] без названий-подстрок друг друга: счёт строк по названию не двоится."""
    chosen = []
    for pid, name in pairs:
        if pid in banned or "<" in name or any(name in n or n in name for _, n in chosen):
            continue
        chosen.append((pid, name))
    return chosen


def full_view(taken, pairs, covered=21.5, cores=4):
    """Витрина полного размера: строки с разными долями, суммы правдоподобны
    (доля ёмкости ≤ 100 %, ядро-ч ≤ ядер × покрытие), у части строк токенов нет."""
    rows = []
    for index, (pid, _) in enumerate(pairs):
        share = round(8 * 0.9 ** index, 2)
        rows.append(view_row(pid, ядро_ч=round(share / 100 * cores * covered, 3), доля_ёмкости_проц=share,
                             память_пик_мб=2000 - 30 * index, память_ср_мб=900 - 10 * index,
                             запусков=200 - 3 * index, в_работе_ч=round(5 * 0.9 ** index, 3),
                             доля_запусков_проц=round(6 * 0.9 ** index, 2),
                             токены=None if index % 5 == 4 else 900000 - 10000 * index,
                             доля_токенов_проц=None if index % 5 == 4 else round(5 * 0.92 ** index, 2)))
    return {"снято": iso(taken), "выключатель": {"ключ": "off", "исключений": 11},
            "окна": {"сейчас": {"покрытие_ч": None, "строки": []},
                     "24ч": {"покрытие_ч": covered, "строки": rows},
                     "7д": {"покрытие_ч": covered, "строки": rows}},
            "сверка": {"расхождение_токенов": 0, "без_метки_проц": 0.0},
            "неразмечено": {"cron.service": 3.2, "вне_корзин": 1.1}}


def parse_decimal(text: str):
    try:
        return float(text.replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


# Браузерная часть — отдельным процессом на python venv playwright: проба
# остаётся на системном python3, где playwright нет.
BROWSER_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright
args = json.loads(sys.argv[1])
with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3,
                                  is_mobile=True, has_touch=True)
    context.add_cookies([{"name": args["cookie"], "value": args["token"], "url": args["base"]}])
    page = context.new_page()
    page.goto(args["base"] + "/processy", wait_until="load")
    height, width, inner = page.evaluate(
        "() => [document.documentElement.scrollHeight, document.documentElement.scrollWidth, window.innerWidth]")
    rows = page.evaluate("""() => { const t = document.querySelector('#cost table');
        if (!t) return [];
        const d = t.closest('details'); if (d) d.open = true;
        return [...t.querySelectorAll('tbody tr')].map(r => r.getBoundingClientRect().height); }""")
    print(json.dumps({"height": height, "width": width, "inner": inner, "rows": rows}))
    browser.close()
'''


# ── стенд ───────────────────────────────────────────────────────────────────
try:
    import server
    import vid
except ImportError as error:
    bad(f"службы панели нет: {error}")
    finish()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            return answer.status, answer.headers.get("Content-Type", ""), answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        return 0, "", f"соединение: {error}"


def conf_text(key):
    # Потолки в конфиге НАРОЧНО не те, что действуют на срезе: страница обязана
    # показывать действующее (cgroup/systemctl), а не задуманное (§Е.3 (3)).
    return (f'BACKGROUND_PROCESSES="{key}"\nCHECKS_CPU_QUOTA=300%\n'
            "CHECKS_MEMORY_HIGH=3G\nCHECKS_MEMORY_MAX=3584M\nPUSH_NEEDS_OWNER=0\n")


with tempfile.TemporaryDirectory(prefix="proba-processy-") as tmp:
    root = Path(tmp)
    logs, cgroup, secrets, libdir = root / "logs", root / "cgroup", root / "secrets", root / "lib"
    for folder in (logs, secrets, libdir):
        folder.mkdir()
    lib, sched, by_mechanism, evil = build_library(libdir)
    records = lib["процессы"]
    exemptions = sched["выключатель"]["исключения"]
    exempt_ids = {e["id"] for e in exemptions}
    check_ids = [r["id"] for r in records if r["запуск"] == "служба-проверок"]
    if not records or len(check_ids) < 3:
        bad(f"БОЛЬНОЙ СЛУЧАЙ: разбор своей библиотеки пуст (записей {len(records)}, служба-проверок {len(check_ids)})")
        finish()
    systemctl = root / "systemctl"
    systemctl.write_text(FAKE_SYSTEMCTL, encoding="utf-8")
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IXUSR)
    running_id, disabled_id = write_checks_fixtures(root, cgroup, check_ids)
    write_runs(logs, check_ids)
    (root / "harness.conf").write_text(conf_text("off"), encoding="utf-8")
    (root / "install.conf").write_text('TG_CHAT_ID="100000001"\n', encoding="utf-8")
    os.environ.update(
        HARNESS_CONF=str(root / "harness.conf"), HARNESS_INSTALL_CONF=str(root / "install.conf"),
        LOG_DIR=str(logs), PANEL_STATE_DIR_LOGS=str(logs), SECRETS_DIR=str(secrets),
        PANEL_SECRETS_DIR=str(secrets), PANEL_STATE_DIR=str(logs),
        HARNESS_PROCESS_LIBRARY=str(libdir / "process-library.yaml"),
        HARNESS_PROCESS_LIBRARY_JSON=str(libdir / "process-library.json"),
        HARNESS_PROCESS_LIBRARY_LOCAL="/nonexistent/harness-probe/process-library.local.json",
        HARNESS_USTROJSTVO=str(libdir / "mechanisms.yaml"),
        PANEL_CGROUP_CHECKS=str(cgroup), PANEL_SYSTEMCTL=str(systemctl),
        PROCESS_STATE_SYSTEMCTL=str(systemctl), PROCESS_STATE_CGROUP=str(root / "state-cgroup"))
    view_path = logs / "process-dispatcher-view.json"
    approx_rec = next(r for r in records if r["id"] == check_ids[0])
    null_rec = next(r for r in records if r["класс"] == "фоновый" and r["id"] not in check_ids)
    week_rec = next(r for r in records if r["класс"] == "фоновый"
                    and r["id"] not in (approx_rec["id"], null_rec["id"]))
    view_ids = (approx_rec["id"], null_rec["id"], week_rec["id"])
    view_path.write_text(json.dumps(build_view(NOW - timedelta(minutes=4), view_ids), ensure_ascii=False),
                         encoding="utf-8")

    server.ЗАПРОСОВ_В_МИНУТУ = 100000  # проба шлёт больше 60 запросов в минуту
    server.Панель.log_message = lambda self, f, *a: sys.stderr.write((f % a) + "\n")
    port = free_port()
    service = server.собрать(порт=port)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    own = {"Origin": base, "Sec-Fetch-Site": "same-origin"}
    login = server.вход()
    pass_req = urllib.request.Request(base + "/vhod", data=json.dumps({"t": login.выдать_токен("100000001")}).encode(),
                                      headers=dict(own, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(pass_req, timeout=10) as answer:
        token = json.loads(answer.read()).get("пропуск")
    authed = dict(own, Cookie=f"{server.ИМЯ_ПРОПУСКА}={token}")

    def page(query=""):
        return get(f"{base}/processy{query}", authed)

    # 1. Маршрут, доступ, якоря, ссылка с главной.
    code, _, body = get(base + "/processy")
    check(code == 401, "БОЛЬНОЙ СЛУЧАЙ: /processy без пропуска закрыт (краснеет: маршрут открыт или его нет)",
          f"код {code}")
    code, _, _ = get(base + "/processy?okno=7d")
    check(code == 401, "без пропуска закрыт и /processy с параметрами", f"код {code}")
    code, ctype, body = page()
    main_code = code
    check(code == 200 and "text/html" in ctype, "с пропуском /processy → 200 text/html (краснеет: маршрута нет)",
          f"код {code} {ctype} {body[:120]!r}")
    parts = blocks(body)
    check(bool(parts), 'три блока по якорям id="lib", id="cost", id="checks" в этом порядке '
          "(краснеет: блок пропал или переставлен)",
          "позиции " + ", ".join(f"{n}={body.find(f'id={chr(34)}{n}{chr(34)}')}" for n in ("lib", "cost", "checks")))
    anchors = set(re.findall(r'href="#(lib|cost|checks)"', body))
    check(anchors == {"lib", "cost", "checks"}, "кнопки-якоря #lib #cost #checks вверху страницы", f"есть {anchors}")
    code, _, main = get(base + "/", authed)
    check(code == 200 and 'href="/processy"' in main and "Процессы" in main,
          "на главной есть кнопка «Процессы» → /processy (краснеет: раздел не в меню)", f"код {code}")
    parts = parts or {"lib": "", "cost": "", "checks": ""}

    # 2. Каждый процесс разбора — ровно одна карточка с его названием; счётчики из разбора.
    lib_cards = cards(parts["lib"])
    expected = collections.Counter(r["название"] for r in records)
    shown = collections.Counter(card_title(c) for cs in lib_cards.values() for c in cs)
    missing = [r["id"] for r in records if len(lib_cards.get(r["id"], [])) != 1]
    check(not missing, f"каждая из {len(records)} записей разбора — ровно одна карточка data-id "
          "(краснеет: список урезан, запись задвоена)", f"нет/не одна: {missing[:6]} всего {len(missing)}")
    check(shown == expected, "заголовок <h3> карточки = «название» записи (краснеет: id вместо названия)",
          f"лишние {list(shown - expected)[:3]} недостающие {list(expected - shown)[:3]}")
    by_class = collections.Counter(r["класс"] for r in records)
    filter_links = links(parts["lib"])
    for cls, count in sorted(by_class.items()):
        hit = [t for q, t in filter_links if q.get("klass") == cls and has_number(t, count)]
        check(bool(hit), f"фильтр klass={cls} ссылкой со счётом {count} из разбора (краснеет: вписанная константа)",
              f"ссылки klass: {[(q.get('klass'), t) for q, t in filter_links if 'klass' in q][:6]}")
    check(any(has_number(t, len(records)) and not q.get("klass") for q, t in filter_links),
          f"фильтр «все» со счётом {len(records)}", "нет ссылки «все N»")
    vkl = [(q, t) for q, t in filter_links if "vkl" in q]
    exempt_vkl = next((q["vkl"] for q, t in vkl if has_number(t, len(exemptions))), None)
    check(exempt_vkl is not None, f"фильтр vkl= «исключения» со счётом {len(exemptions)} из разбора "
          "(краснеет: вписанное 12)", f"ссылки vkl: {[(q.get('vkl'), t) for q, t in vkl]}")
    code, _, body_f = page("?klass=" + urllib.parse.quote("сторонний"))
    third = cards(blocks(body_f).get("lib", ""))
    expected_third = sorted(r["id"] for r in records if r["класс"] == "сторонний")
    check(code == 200 and sorted(third) == expected_third,
          f"?klass=сторонний оставляет ровно {len(expected_third)} карточки сторонних (краснеет: фильтр не работает)",
          f"код {code}, карточки {sorted(third)}")
    if exempt_vkl is None:
        bad("пустой фильтр: нет значения vkl исключений — проверить «Под фильтр ничего не попало» нечем")
    else:
        code, _, body_e = page("?" + urllib.parse.urlencode({"klass": "сторонний", "vkl": exempt_vkl}))
        empty_lib = blocks(body_e).get("lib", "")
        check(code == 200 and not cards(empty_lib) and "Под фильтр ничего не попало" in plain(empty_lib),
              "фильтр без совпадений → «Под фильтр ничего не попало» (краснеет: пустой блок молча)",
              f"код {code}, карточек {len(cards(empty_lib))}")

    # 3. Состояние: исключение, погашен ключом, своя служба, снят.
    exemption = next(e for e in exemptions if any(r["id"] == e["id"] for r in records))
    text = card_text(lib_cards, exemption["id"])
    check(plain(html.escape(exemption["основание"])) in text,
          "исключение показывает своё «основание» (краснеет: основание не выводится)",
          f"{exemption['id']}: {text[:160]!r}")
    plain_bg = next(r for r in records if r["класс"] == "фоновый" and r["id"] not in exempt_ids
                    and r["запуск"] == "крон" and r["статус"].startswith("работ"))
    text_off = card_text(lib_cards, plain_bg["id"])
    check("погашен выключателем" in text_off,
          "фоновый не-исключение при BACKGROUND_PROCESSES=off — «погашен выключателем» "
          "(краснеет: ключ не читается)", f"{plain_bg['id']}: {text_off[:160]!r}")
    checks_card = card_text(lib_cards, check_ids[0])
    check("погашен" not in checks_card and "ключом не гасится" in checks_card,
          "запись служба-проверок при off — не «погашен», а «своя служба, ключом не гасится» "
          "(краснеет: признак запуска не учтён)", f"{check_ids[0]}: {checks_card[:160]!r}")
    removed = next((r for r in records if r["статус"].startswith("снят")), None)
    if removed:
        check(re.search(r'class="[^"]*красная[^"]*"[^>]*>[^<]*снят', "".join(lib_cards.get(removed["id"], []))),
              "снятый процесс — красная пилюля «снят»", removed["id"])
    (root / "harness.conf").write_text(conf_text("on"), encoding="utf-8")
    code, _, body_on = page()
    text_on = card_text(cards(blocks(body_on).get("lib", "")), plain_bg["id"])
    check(code == 200 and "идёт" in text_on and "погашен" not in text_on,
          "тот же фоновый при on — «идёт» (краснеет: ключ прочитан один раз при старте или вписан)",
          f"код {code}: {text_on[:160]!r}")
    (root / "harness.conf").write_text(conf_text("off"), encoding="utf-8")

    # «Что делает»: «зачем» записи, иначе роль механизма по пути.
    with_why = next(r for r in records if r.get("зачем"))
    check(plain(html.escape(with_why["зачем"]))[:40] in card_text(lib_cards, with_why["id"]),
          "«что делает» = поле «зачем» записи", with_why["id"])
    check(ROLE_MARKER in card_text(lib_cards, by_mechanism["id"]),
          "запись без «зачем» берёт «роль» механизма по пути из mechanisms.yaml (дверь HARNESS_USTROJSTVO)",
          f"{by_mechanism['id']}: {card_text(lib_cards, by_mechanism['id'])[:160]!r}")

    # 4. Расписание словами.
    sys.path.insert(0, str(LIB_MODULE.parent))
    try:
        import process_library
        words = getattr(process_library, "schedule_words", None)
    except Exception as error:  # noqa: BLE001
        words = None
        bad(f"process_library не импортируется: {error}")
    if words is None:
        bad("нет функции process_library.schedule_words (контракт кодеру данных, §Е.2)")
    else:
        for cron, want in (("*/10 * * * *", "каждые 10 минут"), ("*/20 * * * *", "каждые 20 минут"),
                           ("40 * * * *", "каждый час в :40"), ("0 6 * * *", "каждый день в 06:00"),
                           ("0 9 * * 2", "по вторникам в 09:00")):
            try:
                got = words(cron)
            except Exception as error:  # noqa: BLE001
                got = f"исключение {error!r}"
            check(got == want, f"schedule_words({cron!r}) = «{want}»", f"получено {got!r}")
        try:
            got = words("@reboot")
        except Exception as error:  # noqa: BLE001
            got = f"исключение {error!r}"
        check("@reboot" in got and "как в кроне" in got,
              "неразобранное расписание — сама строка с «как в кроне» (краснеет: падение или пустота)", repr(got))
    stream_id = next((pid for pid in check_ids if (sched["процессы"].get(pid) or {}).get("расписание")
                      == "*/20 * * * *"), None)
    if stream_id:
        text = card_text(lib_cards, stream_id)
        check("каждые 20 минут" in text and "*/20" not in text,
              "карточка показывает расписание словами, а не крон-строкой «*/20 * * * *»", f"{text[:160]!r}")
    else:
        bad("в своей библиотеке нет записи служба-проверок с расписанием */20 — случай не проверен")
    hook = next(r for r in records if r["запуск"] == "хук")
    check("при событии сессии (хук)" in card_text(lib_cards, hook["id"]),
          "запись без расписания с запуском «хук» — «при событии сессии (хук)»", hook["id"])
    daemon = next(r for r in records if r["запуск"] == "служба")
    check("постоянно" in card_text(lib_cards, daemon["id"]), "запуск «служба» — «постоянно»", daemon["id"])

    # 5. «≈» у оценки — в ячейке «Токены», «—» с причиной у null (спека hr.zapuskov §4.2, §6 перечень).
    rows = table_rows(parts["cost"])
    approx_row = next((r for r in rows if approx_rec["название"] in r[0][1]), None)
    null_row = next((r for r in rows if null_rec["название"] in r[0][1]), None)
    if approx_row is None:
        bad(f"в таблице расходов нет строки «{approx_rec['название']}» (первая ячейка — название из библиотеки)")
    else:
        text, raw = cell(approx_row, "токены")
        check(text is not None and re.search(r"≈\s*11\s*%", text) and 'title="оценка' in raw,
              "доля токенов с точностью «≈» — внутри ячейки «Токены <окно>» как «≈ 11 %» с title «оценка: …» "
              "(краснеет: ≈ потерян или доля в отдельном столбце)", f"ячейка {text!r}")
        text, _ = window_cpu(approx_row)
        check(text is not None and "≈" not in text, "у точного числа «≈» нет", f"ячейка «ЦП <окно>» {text!r}")
    if null_row is None:
        bad(f"в таблице расходов нет строки «{null_rec['название']}»")
    else:
        text, raw = window_cpu(null_row)
        check(text is not None and text.strip() == "—" and 'title="нет данных' in raw,
              "null в витрине → «—» с title «нет данных: …», а не «0» и не «не измерено» "
              "(краснеет: null печатается нулём или старым словом)", f"ячейка {text!r}")
    check(any("Основная сессия Claude" in r[0][1] for r in rows),
          "ярлык claude-session подписан «Основная сессия Claude»", "")
    tds = re.findall(r"<td\b[^>]*>", body)
    check(bool(tds) and all("data-label=" in td for td in tds),
          "у каждой <td> есть data-label (краснеет: таблица не сложится на телефоне)",
          f"td {len(tds)}, без data-label {sum('data-label=' not in t for t in tds)}")
    check("21,5" in plain(parts["cost"]), "покрытие «данных 21,5 ч из 24» с запятой", "")
    sort_keys = {q.get("sort") for q, _ in links(parts["cost"])}
    check({"cpu", "mem", "runs", "tok"} <= sort_keys, "сортировка ссылками ?sort=cpu|mem|runs|tok", f"{sort_keys}")
    check(bool(parts["cost"]) and "витрина устарела" not in plain(parts["cost"]),
          "свежая витрина (4 мин) не помечена устаревшей", "блока расходов нет")
    code, _, body_w = page("?okno=7d")
    check(code == 200 and week_rec["название"] in plain(blocks(body_w).get("cost", "")),
          "?okno=7d показывает строки окна 7д", f"код {code}")
    code, _, body_n = page("?okno=sejchas")
    cost_n = blocks(body_n).get("cost", "")
    labels_n = sorted(set(re.findall(r'data-label="([^"]*)"', cost_n)))
    check(code == 200 and approx_rec["название"] in plain(cost_n) and "ЦП 24 ч" in labels_n
          and "okno=sejchas" not in cost_n,
          "?okno=sejchas → 200 и окно 24 ч, вкладки «сейчас» нет: 10 минут — столбец таблицы "
          "(краснеет: старая вкладка «Сейчас»)", f"код {code}, подписи {labels_n[:9]}")
    # Окно без строк даёт пустая витрина и библиотека без постоянных, плановых и загрузочных записей.
    mini = root / "mini-library.json"
    mini.write_text(json.dumps({"процессы": [r for r in records if r["запуск"] in
                                            ("хук", "команда-сессии", "агент-сессии", "вызов-модели")]},
                               ensure_ascii=False), encoding="utf-8")
    empty_view = build_view(NOW - timedelta(minutes=4), view_ids)
    for window_rows in empty_view["окна"].values():
        window_rows["строки"] = []
    view_path.write_text(json.dumps(empty_view, ensure_ascii=False), encoding="utf-8")
    os.environ["HARNESS_PROCESS_LIBRARY_JSON"] = str(mini)
    code, _, body_e0 = page()
    os.environ["HARNESS_PROCESS_LIBRARY_JSON"] = str(libdir / "process-library.json")
    view_path.write_text(json.dumps(build_view(NOW - timedelta(minutes=4), view_ids), ensure_ascii=False),
                         encoding="utf-8")
    check(code == 200 and "За это окно данных пока нет" in plain(blocks(body_e0).get("cost", "")),
          "окно без строк → «За это окно данных пока нет»", f"код {code}")
    code, _, body_x = page("?okno=zzz")
    check(code == 200 and approx_rec["название"] in plain(blocks(body_x).get("cost", "")),
          "неизвестное окно → 24ч, а не 500", f"код {code}")
    view_path.write_text(json.dumps(build_view(NOW - timedelta(minutes=45), view_ids), ensure_ascii=False),
                         encoding="utf-8")
    code, _, body_s = page()
    check(code == 200 and "витрина устарела" in plain(blocks(body_s).get("cost", "")),
          "витрина старше 30 мин → «витрина устарела»", f"код {code}")

    # 7. Утечки и экранирование (на полной странице с витриной).
    for marker in (SECRET, TOKEN_FLAG, "cmdline"):
        check(main_code == 200 and bool(parts["lib"]) and marker not in body and marker not in body_on,
              f"БОЛЬНОЙ СЛУЧАЙ: «{marker}» из команды/cmdline подделки нет в HTML (краснеет: вывод команды)",
              f"код страницы {main_code}, позиция маркера {body.find(marker)}")
    check("<script" not in body and html.escape(EVIL_TITLE) in body,
          "название с <script> выведено текстом, тегов script на странице нет", "")

    # 8. Служба проверок: срез и таймеры из подделок, не из конфига.
    checks_plain = plain(parts["checks"])
    for pid in check_ids:
        name = next(r["название"] for r in records if r["id"] == pid)
        check(name in checks_plain, f"таймер «{name}» есть в блоке проверок (список — разбором библиотеки)", "")
    check(bool(parts["checks"]) and not re.search(r"<h\d[^>]*>[^<]*harness-[\w-]+\.(timer|service)", parts["checks"]),
          "имена юнитов не в заголовках", "")
    check("1,6 ГБ из 2,5 ГБ" in checks_plain, "срез: «1,6 ГБ из 2,5 ГБ» из memory.current/memory.max",
          checks_plain[:200])
    check("200 %" in checks_plain and "300 %" not in checks_plain,
          "потолок ядер «200 %» — действующий со среза, не 300 % из конфига (краснеет: показ из harness.conf)", "")
    check("мягкий 2 ГБ" in checks_plain and "жёсткий 2,5 ГБ" in checks_plain, "память: мягкий 2 ГБ / жёсткий 2,5 ГБ")
    check("торможений по ядрам: 7" in checks_plain, "торможений по ядрам из cpu.stat nr_throttled")
    check("успешно" in checks_plain and "код 3" in checks_plain,
          "последний прогон: rc 0 → «успешно», rc 3 → «код 3» (из process-runs.jsonl)")
    check("идёт с" in checks_plain, "таймер, чья служба идёт сейчас — «идёт с …»", running_id)
    check("выключен" in checks_plain, "невключённый таймер — «выключен»", disabled_id)
    write_checks_fixtures(root, cgroup, check_ids, memory_max="max")
    code, _, body_m = page()
    check(code == 200 and re.search(r'class="[^"]*красная[^"]*"[^>]*>[^<]*без потолка',
                                    blocks(body_m).get("checks", "")),
          "memory.max = max → красная «без потолка»", f"код {code}")
    write_checks_fixtures(root, cgroup, check_ids, installed=False)
    for name in os.listdir(cgroup):
        (cgroup / name).unlink()
    code, _, body_u = page()
    check(code == 200 and "Служба проверок ещё не установлена" in plain(blocks(body_u).get("checks", "")),
          "нет юнитов и среза → «Служба проверок ещё не установлена», страница 200", f"код {code}")
    write_checks_fixtures(root, cgroup, check_ids)

    # 9. Вёрстка статически (снимки — приёмка Е.5, не здесь).
    common = vid.СТИЛЬ.strip()
    section_css = "\n".join(s for s in re.findall(r"<style[^>]*>(.*?)</style>", body, re.S) if s.strip() != common)
    check(bool(section_css.strip()), "у раздела свой стиль (PROCESS_STYLE)", "нет <style> сверх общего")
    loose = outside_min_width_media(section_css) + ";" + " ".join(re.findall(r'style="([^"]*)"', body))
    wide = [m.group(0) for m in re.finditer(r"(?<![\w-])(?:min-)?width\s*:\s*(\d+)px", loose) if int(m.group(1)) > 360]
    check(bool(section_css.strip()) and not wide, "нет width/min-width > 360px вне @media (min-width…) (краснеет: фиксированная ширина)", f"{wide}")
    wide_grid = [m.group(0) for m in re.finditer(r"minmax\(\s*(\d+)px", loose) if int(m.group(1)) > 354]
    check(bool(section_css.strip()) and not wide_grid, "колонка сетки не шире 354px (390 − поля 2×18) вне @media (min-width…)", f"{wide_grid}")
    narrow = [m for m in re.finditer(r"@media[^{]*max-width\s*:\s*(\d+)px[^{]*\{", section_css)
              if int(m.group(1)) <= 600]
    folded = any(re.search(r"(?:table|tbody|tr|td)[^{]*\{[^}]*display\s*:\s*(?:block|grid|flex)", inner)
                 and "attr(data-label)" in inner
                 for inner in (media_body(section_css, m)[0] for m in narrow))
    check(folded, "@media (max-width:600px): строки таблицы → блоки, подпись из attr(data-label)",
          f"узких медиазапросов {len(narrow)}")

    # 10. Лента на телефоне: библиотека — группы по классу, свёрнутые по умолчанию.
    class_of = {r["id"]: r["класс"] for r in records}
    groups = library_groups(parts["lib"])
    problems, group_of = [], {}
    for tag, summary, group_cards in groups:
        classes = {class_of.get(pid) for pid in group_cards}
        cls = next(iter(classes))
        if len(classes) != 1:
            problems.append(f"в группе карточки разных классов {sorted(map(str, classes))}")
        group_of.setdefault(cls, []).append((tag, summary, group_cards))
        if is_open(tag):
            problems.append(f"группа «{cls}» раскрыта по умолчанию")
        first = re.search(r"\d+", summary)
        if not first or int(first.group()) != by_class.get(cls):
            problems.append(f"summary «{summary[:60]}»: первое число не {by_class.get(cls)}")
    for cls, count in by_class.items():
        found = group_of.get(cls, [])
        own = {r["id"] for r in records if r["класс"] == cls}
        if len(found) != 1 or set(found[0][2]) != own:
            problems.append(f"класс «{cls}»: групп {len(found)}, карточек в группе "
                            f"{len(found[0][2]) if found else 0} из {len(own)}")
    check(bool(groups) and not problems,
          "библиотека по умолчанию — по одной <details> без open на класс, в <summary> первым числом счёт "
          "класса из разбора, все карточки класса внутри (краснеет: лента из всех карточек)",
          "; ".join(problems[:4]) or "групп-<details> с карточками нет")

    problems = []
    for cls, found in group_of.items():
        _, summary, group_cards = found[0]
        want = {"идёт": sum(bool(re.search(r'class="[^"]*зелёная[^"]*"[^>]*>\s*идёт\s*<', "".join(c)))
                            for c in group_cards.values()),
                "погашено": sum("погашен выключателем" in "".join(c) for c in group_cards.values()),
                "исключений": len(set(group_cards) & exempt_ids)}
        for word, number in want.items():
            got = re.search(rf"{word}\s+(\d+)", summary)
            if (int(got.group(1)) if got else 0) != number or (number and not got):
                problems.append(f"«{cls}»: {word} {got.group(1) if got else 'нет'} вместо {number}")
    check(bool(group_of) and not problems,
          "итог группы в <summary>: «идёт N · погашено N · исключений N» (нулевое можно не писать), "
          "числа — по карточкам группы и разбору (краснеет: итог вписан или не считан)",
          "; ".join(problems[:4]) or "групп нет")

    third_groups = library_groups(blocks(body_f).get("lib", ""))
    finder = next(r for r in records if r["класс"] == "по внутреннему действию" and r["id"] != evil["id"])
    code_q, _, body_q = page("?" + urllib.parse.urlencode({"q": finder["название"]}))
    found_groups = library_groups(blocks(body_q).get("lib", ""))
    opened_third = [is_open(t) for t, _, c in third_groups if set(c) & set(expected_third)]
    opened_q = [is_open(t) for t, _, c in found_groups if finder["id"] in c]
    check(opened_third == [True] and code_q == 200 and opened_q == [True],
          "фильтр раскрывает свою группу: ?klass=сторонний и ?q=<название> — группа с совпадениями "
          "<details open> (краснеет: найденное спрятано в свёрнутой группе)",
          f"klass: {opened_third}, q (код {code_q}): {opened_q}")

    # 11. Лента: в расходах сверху не больше 10 строк на группу полос и «остальные N — ≈X %».
    labels = set(getattr(vid, "PROCESS_LABELS", {}))
    bar_pairs = distinct_names([(r["id"], r["название"]) for r in records], labels)[:57]
    full = full_view(NOW - timedelta(minutes=4), bar_pairs)
    view_path.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
    code, _, body_full = page()
    visible = without_details(blocks(body_full).get("cost", ""))
    marks = list(re.finditer(r'data-group="(cpu|mem|runs|tok)"', visible))
    bar_fields = {"cpu": "доля_ёмкости_проц", "mem": "память_пик_мб", "runs": "доля_запусков_проц",
                  "tok": "доля_токенов_проц"}
    problems = [] if sorted(m.group(1) for m in marks) == sorted(bar_fields) else [
        f"data-group: {[m.group(1) for m in marks]}"]
    window_rows = full["окна"]["24ч"]["строки"]
    name_of = dict(bar_pairs)
    for index, mark in enumerate(marks):
        group, field = mark.group(1), bar_fields[mark.group(1)]
        begin = visible.rfind("<div", 0, mark.start())
        text = plain(visible[begin:div_end(visible, begin)])
        rated = sorted((r for r in window_rows if r.get(field)), key=lambda r: -r[field])
        shown = {name for name in name_of.values() if name in text}
        top = {name_of[r["id"]] for r in rated[:len(shown)]}
        if not min(8, len(rated)) <= len(shown) <= BAR_MAX_LINES or shown != top:
            problems.append(f"{group}: строк {len(shown)} (нужно 8…{BAR_MAX_LINES}), не из верхних {len(shown - top)}")
            continue
        rest = rated[len(shown):]
        line = re.search(r"остальные\s+(\d+)\s*[—–-]\s*(?:≈\s*)?(\d[\d  ,]*?)\s*(%|МБ)", text)
        if rest and not line:
            problems.append(f"{group}: нет строки «остальные {len(rest)} — ≈X %»")
        elif rest and int(line.group(1)) != len(rest):
            problems.append(f"{group}: «остальные {line.group(1)}» вместо {len(rest)}")
        elif rest and group != "mem":
            share, total = parse_decimal(line.group(2)), sum(r[field] for r in rest)
            if line.group(3) != "%" or share is None or abs(share - total) > max(0.5, total / 100):
                problems.append(f"{group}: «≈{line.group(2)} {line.group(3)}» вместо ≈{total:.1f} %")
    visible_rows = table_rows(visible)
    not_green = sum(1 for r in visible_rows
                    if any(w in (cell(r, "состояние")[0] or "") for w in ("упал", "пропущен", "не запущен", "неизвестно")))
    if len(visible_rows) - not_green > 20:
        problems.append(f"вне <details> {len(visible_rows)} строк таблицы: больше 20 плюс {not_green} не-зелёных")
    check(code == 200 and not problems,
          f"расходы на витрине из {len(window_rows)} строк: в каждой группе полос data-group=cpu|mem|runs|tok "
          f"вне <details> верхние 8…{BAR_MAX_LINES} процессов и «остальные N — ≈X %» с верными N и суммой, "
          "вне <details> таблица не длиннее 20 строк плюс не-зелёные (краснеет: лента полос или хвост без счёта)",
          f"код {code}; " + "; ".join(problems[:5]))

    # 12. Высота на телефоне 390×844 в браузере: живая библиотека, витрина полного размера.
    live_records = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))["процессы"]
    live_pairs = [(r["id"], r["название"]) for r in live_records][:57]
    door = {"HARNESS_PROCESS_LIBRARY": str(SOURCE_YAML), "HARNESS_PROCESS_LIBRARY_JSON": str(SOURCE_JSON),
            "HARNESS_PROCESS_LIBRARY_LOCAL": "/nonexistent/harness-probe/process-library.local.json"}
    if SOURCE_MECHANISMS:
        door["HARNESS_USTROJSTVO"] = str(SOURCE_MECHANISMS)
    saved = {k: os.environ[k] for k in door}
    os.environ.update(door)
    view_path.write_text(json.dumps(full_view(NOW - timedelta(minutes=4), live_pairs), ensure_ascii=False),
                         encoding="utf-8")
    measured, why = None, ""
    if len(live_records) < 70 or len(live_pairs) < 50:
        why = f"БОЛЬНОЙ СЛУЧАЙ: живой разбор мал — записей {len(live_records)}, строк витрины {len(live_pairs)}"
    elif not Path(PLAYWRIGHT_PYTHON).is_file():
        why = f"нет python с playwright: {PLAYWRIGHT_PYTHON} (дверь PANEL_PLAYWRIGHT_PYTHON) — высоту мерить нечем"
    else:
        script = root / "browser_height.py"
        script.write_text(BROWSER_SCRIPT, encoding="utf-8")
        args = json.dumps({"base": base, "cookie": server.ИМЯ_ПРОПУСКА, "token": token})
        try:
            done = subprocess.run([PLAYWRIGHT_PYTHON, str(script), args], capture_output=True, text=True, timeout=240)
            measured = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else None
            why = "" if measured else f"браузер rc {done.returncode}: {done.stderr.strip()[-300:]}"
        except (subprocess.TimeoutExpired, ValueError, IndexError) as error:
            why = f"браузер не ответил: {error!r}"
    os.environ.update(saved)
    if measured is None:
        bad(f"высота /processy на 390×844 не измерена — {why}")
        bad(f"строка «Все процессы» на 390 не измерена — {why}")
    else:
        check(measured["height"] <= PHONE_MAX_HEIGHT and measured["width"] == measured["inner"],
              f"/processy на 390×844 (библиотека {len(live_records)}, витрина {len(live_pairs)} строк): "
              f"scrollHeight ≤ {PHONE_MAX_HEIGHT} и scrollWidth = innerWidth (краснеет: лента карточек и полос)",
              f"scrollHeight {measured['height']}, scrollWidth {measured['width']}/{measured['inner']}")
        heights = measured["rows"]
        average = sum(heights) / len(heights) if heights else 0
        check(bool(heights) and average <= PHONE_MAX_ROW,
              f"строка таблицы «Все процессы» на 390 в среднем ≤ {PHONE_MAX_ROW} px — «подпись | значение» "
              "в одну строку (краснеет: подпись над значением, 9 пар столбиком)",
              f"строк {len(heights)}, средняя {average:.0f} px, наибольшая {max(heights, default=0):.0f} px")

    # 13. Ревью Е, блокер 1: битая строка витрины не роняет страницу.
    timer_names = [next(r["название"] for r in records if r["id"] == pid) for pid in check_ids]

    def page_survives(view):
        view_path.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
        code, _, body_v = page()
        shown = blocks(body_v)
        whole = (bool(shown) and set(cards(shown["lib"])) >= set(class_of)
                 and all(n in plain(shown["checks"]) for n in timer_names))
        return code == 200 and whole and "витрин" in plain(shown["cost"]).lower(), f"код {code}, блоков {len(shown)}"

    no_id = build_view(NOW - timedelta(minutes=4), view_ids)
    no_id["окна"]["24ч"]["строки"].append({k: v for k, v in view_row("x").items() if k != "id"})
    passed, detail = page_survives(no_id)
    check(passed, "строка витрины без id → 200, библиотека и проверки целы, в расходах причина со словом "
          "«витрина» (краснеет: KeyError рвёт соединение)", detail)
    as_text = build_view(NOW - timedelta(minutes=4), view_ids)
    as_text["окна"]["24ч"]["строки"].append(view_row(by_mechanism["id"], доля_ёмкости_проц="12", запусков="12"))
    passed, detail = page_survives(as_text)
    check(passed, "число строкой «12» в витрине → 200, библиотека и проверки целы, в расходах причина со словом "
          "«витрина» (краснеет: TypeError рвёт соединение)", detail)

    # 14. Ревью Е, блокер 2: невозможное число — «сбой замера», а не факт.
    def cost_of(rows, covered, unmarked=None, **window):
        view = build_view(NOW - timedelta(minutes=4), view_ids)
        view["окна"]["24ч"] = {"покрытие_ч": covered, "строки": rows, **window}
        view["неразмечено"] = unmarked or view["неразмечено"]
        view_path.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
        code, _, body_v = page()
        raw = blocks(body_v).get("cost", "")
        return code, raw, plain(raw)

    failure_pill = r'class="[^"]*красная[^"]*"[^>]*>[^<]*сбой замера'
    code, raw, text = cost_of([view_row(approx_rec["id"], доля_ёмкости_проц=9850.0, ядро_ч=3.0),
                               view_row(week_rec["id"], доля_ёмкости_проц=25.0, ядро_ч=0.5)], 0.8)
    leaked = [n for n in ("9850", "9 850", "9875", "9 875") if n in text]
    check(code == 200 and re.search(failure_pill, raw) and not leaked and week_rec["название"] in text,
          "доля ёмкости 9850 % → красная пилюля «сбой замера», числа 9 850 и суммы 9 875 нет, "
          "соседняя строка на месте (краснеет: бессмыслица как факт)", f"код {code}, напечатано {leaked}")
    code, raw, text = cost_of([view_row(approx_rec["id"], доля_ёмкости_проц=40.0, ядро_ч=321.0),
                               view_row(week_rec["id"], доля_ёмкости_проц=25.0, ядро_ч=0.5)], 0.8)
    check(code == 200 and re.search(failure_pill, raw) and not has_number(text, 321),
          "ядро-ч 321 при покрытии 0,8 ч (> ядер × покрытие) → «сбой замера», числа 321 нет "
          "(краснеет: невозможное ядро-ч как факт)", f"код {code}, 321 в тексте: {has_number(text, 321)}")

    # 15. Ревью Е, важная 3: подвал и строка юнита не противоречат друг другу.
    code, raw, text = cost_of([view_row(week_rec["id"], доля_ёмкости_проц=20.0),
                               view_row("cron.service: неразмечено", доля_ёмкости_проц=2.6, ядро_ч=0.2)], 21.5,
                              {"cron.service": 0.0, "вне_корзин": 1.1})
    footer = [parse_decimal(v) for v in re.findall(r"cron\.service:?\s+(\d[\d,]*)\s*%", text)]
    check(code == 200 and footer and all(v is not None and v >= 2.55 for v in footer),
          "подвал «не разобрано: cron.service X %» не меньше строки «Не разобрано (cron.service) 2,6 %» того же "
          "окна (краснеет: «cron.service 0 %» под «≈2,6 %»)", f"код {code}, в подвале {footer}")

    # 16. Ревью Е, важная 4: ярлыки не из библиотеки — словами, не сырыми.
    code, raw, text = cost_of([view_row(week_rec["id"]), view_row("крон:run-background.sh"), view_row("прочее")], 21.5)
    check(code == 200 and "Задание крона без метки (run-background.sh)" in text
          and "Прочие процессы сервера" in text and "крон:" not in text,
          "ярлык «крон:<x>» → «Задание крона без метки (<x>)», «прочее» → «Прочие процессы сервера», "
          "сырого «крон:» нет (краснеет: ярлык как есть)", f"код {code}, «крон:» в тексте: {'крон:' in text}")

    # 17. Ревью Е, важная 7: память — пик, а не сумма пиков разных процессов.
    code, raw, text = cost_of([view_row(week_rec["id"], память_пик_мб=200), view_row(null_rec["id"], память_пик_мб=300)],
                              21.5, память_пик_мб=1234)
    check(code == 200 and "сумма пиков" not in text and not re.search(r"(?<!\d)500 МБ", text)
          and ("Память (пик)" not in text or "1 234" in text),
          "плитка памяти не «сумма пиков»: суммы 500 МБ нет; «Память (пик)» берёт пик окна «память_пик_мб» "
          "(1 234 МБ) или плитки нет (краснеет: сложены пики разных минут)", f"код {code}")

    # 18. Ревью Е, важная 8: покрытие замера сервера не подписывает запуски и токены.
    view_path.write_text(json.dumps(build_view(NOW - timedelta(minutes=4), view_ids), ensure_ascii=False),
                         encoding="utf-8")
    code, _, body_c = page()
    text = plain(blocks(body_c).get("cost", ""))
    core_at = text.find("Ядра")
    runs_at = text.find("Запуски", core_at + 1)
    spots = [m.start() for m in re.finditer(r"21,5", text)]
    check(code == 200 and 0 <= core_at < runs_at and spots and all(core_at < s < runs_at for s in spots),
          "«данных 21,5 ч» стоит у плиток «Ядра»/«Память» (между «Ядра» и «Запуски»), а не строкой над всеми "
          "плитками (краснеет: покрытие сервера подписывает запуски и токены)",
          f"код {code}, «Ядра» {core_at}, «Запуски» {runs_at}, «21,5» {spots}")

    # 19. Ревью Е, важная 5: сбой сборки витрины не оставляет временный файл.
    if not DISPATCHER:
        bad("scripts/process-dispatcher.py не найден — сбой сборки витрины проверить нечем")
    else:
        view_dir = root / "view-fail"
        view_dir.mkdir()
        broken = root / "broken-library.json"
        broken.write_text(json.dumps({"процессы": [{}]}), encoding="utf-8")
        env = dict(os.environ, LOG_DIR=str(view_dir), HARNESS_LOG_DIR=str(view_dir))
        done = subprocess.run([sys.executable, str(DISPATCHER), "--витрина", "--журналы", str(view_dir),
                               "--библиотека", str(broken), "--транскрипты", str(root / "no-transcripts"),
                               "--proc", str(root / "no-proc")], capture_output=True, text=True, env=env, timeout=120)
        left = sorted(p.name for p in view_dir.glob("process-dispatcher-view.json.*"))
        if done.returncode == 0:
            bad("БОЛЬНОЙ СЛУЧАЙ: дверь сбоя закрылась — библиотека с записью без id больше не роняет --витрина; "
                "нужна другая дверь сбоя build_view")
        else:
            check(not left, "--витрина упала на сборке (запись библиотеки без id) → огрызка "
                  "process-dispatcher-view.json.XXXX в каталоге журналов нет (краснеет: mkstemp до сборки без unlink)",
                  f"осталось {left}")

    # 20. Состояние процесса в таблице расходов (спека hr.zapuskov-none-net-informacii-znachit §4.2–4.3, §6 п.13).
    values = state_values()
    check(bool(values), "константа VALUES разобрана из scripts/lib/process_state.py", f"модуль {STATE_MODULE}")
    by_id = {r["id"]: r for r in records}
    panel_rec, dispatcher_rec = by_id.get("harness-panel"), by_id.get("harness-dispatcher")
    if not (panel_rec and dispatcher_rec):
        bad("БОЛЬНОЙ СЛУЧАЙ: в своей библиотеке нет harness-panel / harness-dispatcher — постоянные службы не проверить")
    else:
        show_table = json.loads((root / "show.json").read_text(encoding="utf-8"))
        show_table["harness-panel.service"] = {"LoadState": "loaded", "ActiveState": "failed", "SubState": "failed",
                                               "Result": "exit-code", "ExecMainStatus": "1"}
        show_table["harness-dispatcher.service"] = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                                                    "Result": "success", "ExecMainStatus": "0"}
        (root / "show.json").write_text(json.dumps(show_table), encoding="utf-8")
        (root / "failed.json").write_text(json.dumps([{"unit": "broken.service", "load": "loaded", "active": "failed",
                                                       "sub": "failed", "description": "проба"}]), encoding="utf-8")
        busy = [view_row(f"probe-proc-{n}", доля_ёмкости_проц=10.0 + n, ядро_ч=0.1 + n / 100) for n in range(25)]
        system = [view_row(unit, доля_ёмкости_проц=0.5, ядро_ч=0.01) for unit in ("nginx.service", "snapd.service",
                                                                                     "broken.service")]
        view = build_view(NOW - timedelta(minutes=4), view_ids)
        view["окна"]["24ч"]["строки"] += busy + system + [view_row("harness-dispatcher", доля_ёмкости_проц=0.01,
                                                                    ядро_ч=0.001)]
        missed_rec = next((r for r in records if r["запуск"] == "крон" and r.get("статус") == "работает"
                           and r["класс"] == "фоновый"), None)
        missed_at = NOW - timedelta(hours=3)
        view["state_facts"] = {"checked": iso(NOW - timedelta(minutes=4)), "docker": {}, "docker_error": None,
                               "missed": {missed_rec["id"]: iso(missed_at)} if missed_rec else {},
                               "log_start": iso(NOW - timedelta(days=2)), "last_run": {}}
        view_path.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
        code, _, body_t = page()
        cost_t = blocks(body_t).get("cost", "")
        visible, hidden, summary = more_details(cost_t)
        shown_rows, hidden_rows, all_rows = table_rows(visible), table_rows(hidden), table_rows(cost_t)

        def state_of(row):
            return cell(row, "состояние")[0] or ""

        def named(rows, name):
            return next((r for r in rows if r[0][1].strip().startswith(name)), None)
        first = shown_rows[0] if shown_rows else None
        check(code == 200 and first is not None and "упал" in state_of(first),
              "без ?sort первая строка таблицы — «упал» (краснеет: порядок по ЦП прячет упавшее)",
              f"код {code}, первая строка {first[0][1] if first else None!r}: {state_of(first) if first else ''!r}")
        row = named(all_rows, panel_rec["название"])
        check(row is not None and "упал" in state_of(row) and "exit-code" in " ".join(t for _, t, _ in row),
              "постоянная служба с юнитом failed — метка «упал» и причина (Result, код) в тексте строки",
              f"строка {row and [t for _, t, _ in row][:3]!r}")
        row = named(shown_rows, dispatcher_rec["название"])
        check(row is not None and "не запущен" in state_of(row),
              "«не запущен» у постоянной при 25 строках с большим ЦП стоит выше «ещё N» (краснеет: хвост прячет stopped)",
              f"видимых строк {len(shown_rows)}, в видимых: {row is not None}")
        row = named(shown_rows, missed_rec["название"]) if missed_rec else None
        said = state_of(row) if row else ""
        raw_state = cell(row, "состояние")[1] if row else ""
        missed_clock = f"{missed_at:%H:%M}" if missed_at.date() == NOW.date() else f"{missed_at:%d.%m %H:%M}"
        why_title = f'title="плановый {missed_clock} не состоялся"'
        check(row is not None and "пропущен" in said and f"плановый {missed_clock} не состоялся" in said
              and re.search(r"проверено \d+ мин назад", said) and why_title in raw_state,
              "крон с missed в витрине — «пропущен · плановый ЧЧ:ММ не состоялся · проверено N мин назад», полный текст "
              "причины в title (краснеет: нет подписи свежести или причина режется без title на телефоне)",
              f"крон {missed_rec and missed_rec['id']}: {said!r}")
        more = re.match(r"ещё\s+(\d+)\s+процесс", summary)
        check(bool(more) and int(more.group(1)) == len(hidden_rows) and len(shown_rows) >= 20,
              "сверху ≥ 20 строк, остальное под <details> «ещё N процессов — …», N = скрытым строкам",
              f"summary {summary!r}, видимых {len(shown_rows)}, скрытых {len(hidden_rows)}")
        hidden_bad = [r[0][1] for r in hidden_rows if any(w in state_of(r) for w in ("упал", "пропущен", "не запущен"))]
        check(bool(hidden_rows) and not hidden_bad, "под «ещё N» нет строк «упал / пропущен / не запущен»", f"{hidden_bad[:4]}")
        tables = "".join(re.findall(r"<table\b.*?</table>", cost_t, re.S))
        check(bool(tables) and "не измерено" not in plain(tables),
              "в таблице расходов слова «не измерено» нет (пусто — «—»)", f"раз: {plain(tables).count('не измерено')}")
        labels_t = set(re.findall(r'data-label="([^"]*)"', tables))
        check({"Состояние", "Запуски", "ЦП 10 мин", "ЦП 24 ч"} <= labels_t and any(l.startswith("Токены") for l in labels_t),
              "столбцы: Название · Состояние · Запуски · ЦП 10 мин · ЦП 24 ч · Память · Токены 24 ч (data-label)",
              f"{sorted(labels_t)}")
        names_t = [r[0][1].strip() for r in all_rows]
        rollup = [n for n in names_t if n.startswith("Системные службы сервера (")]
        check(len(rollup) == 1 and re.match(r"Системные службы сервера \(2\)", rollup[0] or "")
              and not any(n.startswith(("nginx.service", "snapd.service")) for n in names_t),
              "nginx.service и snapd.service — одной строкой «Системные службы сервера (2)», отдельных строк нет",
              f"свёртка {rollup}, строки системных {[n for n in names_t if '.service' in n][:4]}")
        row = named(all_rows, "broken.service")
        check(row is not None and "упал" in state_of(row), "упавший системный юнит — отдельной строкой «упал», не в свёртке",
              f"строка {row and row[0][1]!r}")
        code, _, body_w7 = page("?okno=7d")
        labels_7 = set(re.findall(r'data-label="([^"]*)"', blocks(body_w7).get("cost", "")))
        check(code == 200 and "ЦП 7 сут" in labels_7 and "ЦП 10 мин" in labels_7,
              "?okno=7d — длинное окно 7 сут вторым столбцом ЦП, «10 мин» на месте", f"код {code}, {sorted(labels_7)[:8]}")
        try:
            import state
            snapshot_states = state.processes().get("состояния")
        except Exception as error:  # noqa: BLE001
            snapshot_states = f"{type(error).__name__}: {error}"
        own = [r["id"] for r in records if r["запуск"] != "хук"]
        lost = [i for i in own if not isinstance(snapshot_states, dict) or i not in snapshot_states
                or (snapshot_states[i] or {}).get("value") not in values]
        check(bool(values) and isinstance(snapshot_states, dict) and not lost,
              f"state.processes()['состояния']: каждая из {len(own)} записей не-хуков с value из VALUES (§5 п.1)",
              f"{snapshot_states if not isinstance(snapshot_states, dict) else lost[:5]}")
        (root / "fail.txt").write_text(f"Failed to connect to bus {TOKEN_FLAG}={SECRET}\n", encoding="utf-8")
        code, _, body_f2 = page()
        (root / "fail.txt").unlink()
        shown_f = blocks(body_f2)
        check(code == 200 and SECRET not in body_f2 and TOKEN_FLAG not in body_f2 and bool(cards(shown_f.get("lib", "")))
              and "systemctl" in plain(shown_f.get("cost", "")),
              "systemctl упал с «--token=…» в stderr → страница 200, причина «systemctl: …» без аргументов, "
              "маркера нет в HTML, библиотека на месте (краснеет: why выводится дословно)",
              f"код {code}, маркер в HTML: {SECRET in body_f2}")
        (root / "failed.json").write_text("[]", encoding="utf-8")
        write_checks_fixtures(root, cgroup, check_ids)

    # 20а. Идущий запуск (открытый start, rc ещё нет) в «Запусках» — не провал.
    going = vid._p_last_run({"at": iso(NOW - timedelta(minutes=1)), "rc": None, "outcome": "running", "seconds": None},
                            NOW.timestamp())
    check("✗" not in going and "идёт" in going,
          "идущий запуск в ячейке «Запуски» — «идёт с ЧЧ:ММ», не «✗» (краснеет: rc None подписан провалом)",
          f"{going!r}")

    # 20б. Момент не сегодня — с датой: «посл. 07:00» двухдневной давности читается как сегодняшний.
    old_at = NOW - timedelta(days=2)
    old_run = vid._p_last_run({"at": iso(old_at), "rc": 0, "outcome": "end", "seconds": 5}, NOW.timestamp())
    today_run = vid._p_last_run({"at": iso(NOW), "rc": 0, "outcome": "end", "seconds": 5}, NOW.timestamp())
    check(f"посл. {old_at:%d.%m %H:%M}" in old_run and f"посл. {NOW:%H:%M} ✓" in today_run,
          "«посл. …» не сегодня — «ДД.ММ ЧЧ:ММ», сегодня — «ЧЧ:ММ» (краснеет: дата потеряна или лишняя)",
          f"{old_run!r} / {today_run!r}")

    # 21. Блок «Служба проверок»: прогон с кодом лимита подписан той же функцией process_state (§4.4, §6 п.14).
    limit_found = re.search(r"^KOD_LIMITA=(\d+)", DEMON_SCRIPT.read_text(encoding="utf-8"), re.M) if DEMON_SCRIPT else None
    demon_check = next((i for i in check_ids if ((by_id[i].get("зовёт_модель") or {}).get("через")
                                                 == "scripts/claude-demon.sh")), None)
    plain_check = next((i for i in check_ids if i != demon_check and not by_id[i].get("зовёт_модель")), None)
    if not (limit_found and demon_check and plain_check):
        bad(f"БОЛЬНОЙ СЛУЧАЙ: нет KOD_LIMITA ({DEMON_SCRIPT}) или проверки-демона/обычной проверки в своей библиотеке")
    else:
        limit = int(limit_found.group(1))
        write_runs(logs, check_ids)
        moment = NOW - timedelta(minutes=2)
        extra = [{"v": 1, "ts": iso(moment), "src": "scripts/run-background.sh", "event": "process.end",
                  "outcome": "error", "data": {"id": pid, "pid": 222, "rc": rc, "с": 8.0, "cpu_с": 1.0}}
                 for pid, rc in ((demon_check, limit), (plain_check, 1))]
        with open(logs / "process-runs.jsonl", "a", encoding="utf-8") as runs_log:
            runs_log.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in extra))
        code, _, body_k = page()
        checks_k = blocks(body_k).get("checks", "")
        rows_k = table_rows(checks_k)

        def check_row(pid):
            name = by_id[pid]["название"]
            return next((" ".join(t for _, t, _ in r) for r in rows_k if r[0][1].strip().startswith(name)), "")
        demon_text, plain_text = check_row(demon_check), check_row(plain_check)
        check(code == 200 and "пропущен: лимит подписки" in demon_text and f"код {limit}" not in plain(checks_k),
              f"прогон демона модели с rc {limit} (KOD_LIMITA) → «пропущен: лимит подписки», «код {limit}» в блоке нет "
              "(краснеет: блок подписывает код мимо process_state)", f"код {code}, строка {demon_text[:160]!r}")
        check("код 1" in plain_text, "прогон обычной проверки с rc 1 → «код 1», как прежде", f"строка {plain_text[:160]!r}")
        write_runs(logs, check_ids)

    # 6. Пустые и битые данные — последними: портят витрину и журнал.
    view_path.unlink()
    code, _, body_0 = page()
    p0 = blocks(body_0)
    check(code == 200 and "Данных пока нет" in plain(p0.get("cost", "")) and bool(cards(p0.get("lib", "")))
          and bool(p0.get("checks")),
          "витрины нет → «Данных пока нет», страница 200, библиотека и проверки на месте "
          "(краснеет: нет витрины роняет страницу или весь сбор)", f"код {code}")
    try:
        import state
        snapshot = state.processes()
        shape = set(snapshot) >= {"библиотека", "диспетчер", "проверки", "снято"}
        check(shape and "нет данных" in snapshot["диспетчер"] and "нет данных" not in snapshot["библиотека"]
              and "нет данных" not in snapshot["проверки"],
              "state.processes(): без витрины «нет данных» только у диспетчера (краснеет: сбой одного роняет все)",
              f"ключи {sorted(snapshot)[:6]}")
    except Exception as error:  # noqa: BLE001
        bad(f"state.processes() недоступна или падает: {error!r}")
    write_runs(logs, check_ids, lines=False)
    code, _, body_r = page()
    check(code == 200 and "прогонов ещё не было" in plain(blocks(body_r).get("checks", "")),
          "пустой process-runs.jsonl → «прогонов ещё не было»", f"код {code}")
    view_path.write_text("{битый json", encoding="utf-8")
    code, _, body_b = page()
    pb = blocks(body_b)
    check(code == 200 and "витрин" in plain(pb.get("cost", "")).lower() and bool(pb.get("checks")),
          "битая витрина → причина (со словом «витрина») в блоке расходов, а не 500", f"код {code}")
    view_path.write_text("", encoding="utf-8")
    code, _, body_z = page()
    check(code == 200 and bool(blocks(body_z)), "пустой файл витрины → страница 200 с тремя блоками", f"код {code}")

    service.shutdown()

finish()
