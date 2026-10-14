#!/usr/bin/env python3
"""Проверка канбана панели: доска, страница задачи, просмотр бумаги, комментарии.

Спека: docs/specs/hr.kanban.md (§3.3 формат снимка, §4 комментарии, §5
страница задачи и бумага, §7.2–7.4 приёмка). Контракт:
.tasks/hr.kanban/pretask.yaml.

Служба поднимается на свободном порту, снимок доски — своя фикстура
(`PANEL_BOARD`), каталог состояния — свой (`PANEL_STATE_DIR`), журналы — свои
(`LOG_DIR`). Живая карта, боевые журналы и живая служба не трогаются: проверка
СОЗДАЁТ своё условие, а не спрашивает машину.

Прогон (после раскладки в проект):
    python3 harness/panel/test_task_board.py
"""
import datetime
import http.client
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

paths_total = 0
failures = 0


def path_ok(text):
    global paths_total
    paths_total += 1
    print("ок  · " + text)


def path_bad(text):
    global paths_total, failures
    paths_total += 1
    failures += 1
    print("ПЛОХО · " + text)


def summary_and_exit():
    print(f"КАНБАН-ПАНЕЛИ: путей {paths_total}, неудач {failures}")
    sys.exit(1 if failures else 0)


# Отправка в канал выключена до импорта службы: запасной вход в этой проверке
# иначе написал бы владельцу настоящее сообщение «кто-то входит запасным кодом».
os.environ["PANEL_TG_SEND"] = ""

try:
    import server
    import login
except Exception as error:   # служба не поднимается — это ответ, а не трассировка
    path_bad(f"службы панели нет: {error}")
    summary_and_exit()

HARNESS_CONF_TEXT = """PUSH_NEEDS_OWNER=0
DEPLOY_NEEDS_OWNER=1
PUBLISH_NEEDS_OWNER=1
AUTOMATION_LEVEL="partial"
"""

# Колонки — полный пайплайн, как их отдаёт снимок. Панель обязана брать их из
# снимка, а не из своего списка: «мутации» здесь нарочно пустая — она рисуется
# словом «пусто».
COLUMNS = ["очередь", "спека", "ревью спеки", "план", "контракт", "тесты", "код",
           "ревью кода", "мутации", "живая проверка", "закрытие"]
FILLED_COLUMNS = [c for c in COLUMNS if c != "мутации"]
TASK_COUNT = 45                     # больше прежней обрезки [:40]
DETAIL_ID = "hr.proba"
XSS_TITLE = "<script>alert('проба')</script>"
SOURCE_TEXT = "мне нужно визуализировать карту разработки в канбане — проба"
JOURNAL_TEXT = "Журнал пробы: спека согласована, тесты красные."
COMMIT_HASH = "abc1234"
COMMIT_SUBJECT = "Канбан: проба коммита задачи"
SKILL_NAME = "spec-writing"
PAPER_TEXT = "<b>спека пробы</b> & текст бумаги"
OUTSIDE_SECRET = "СЕКРЕТ-СНАРУЖИ-КОРНЯ"  # не-секрет: образец файла вне корня бумаг


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def request(url, data=None, headers=None, method=None, form=None):
    """(код, тело). Отказ сервера — это ответ, а не исключение."""
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
    else:
        body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        return 0, f"нет ответа: {error}"


def raw_get(port, path, headers=None):
    """GET с путём КАК ЕСТЬ: клиент не вправе «починить» `..` или `%2F` за нас,
    иначе проба обхода каталога проверяла бы клиента, а не службу."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as error:
        return 0, f"нет ответа: {error}"
    finally:
        conn.close()


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def raw_post_headers(url, form):
    """Заголовки ответа на форму без перехода: нужен Set-Cookie запасного входа."""
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(req, timeout=10) as response:
            return dict(response.headers)
    except urllib.error.HTTPError as error:
        return dict(error.headers)


def iso_minutes_ago(minutes: float) -> str:
    moment = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes)
    return moment.isoformat(timespec="seconds")


def plain_task(index: int) -> dict:
    return {"id": f"pr.k{index:02d}", "название": f"Карточка-проба {index:02d}",
            "статус": "wip", "колонка": FILLED_COLUMNS[index % len(FILLED_COLUMNS)],
            "пилюли": [], "уровень_объявлен": "П2", "уровень_по_коммитам": "П2",
            "цена": 3, "шаги": [], "источники": [], "журнал": None,
            "коммиты": [], "скилы": []}


def detail_task(papers: dict) -> dict:
    """Задача с полной страницей. Каждая бумага стоит в `шаги` ДВАЖДЫ подряд:
    нечётный номер n попадает в одну и ту же бумагу и при счёте с нуля, и при
    счёте с единицы — проба не навязывает реализации базу номера."""
    steps = []
    for step_name, path in [("спека", papers["normal"]),
                            ("ревью спеки", papers["symlink_out"]),
                            ("контракт", papers["fifo"]),
                            ("план", papers["big"]),
                            ("ревью кода", papers["directory"]),
                            ("мутации", papers["outside_abs"])]:
        steps += [{"шаг": step_name, "путь": str(path), "есть": True, "почему": ""}] * 2
    steps.append({"шаг": "тесты", "путь": None, "есть": None,
                  "почему": "машиной не судится"})
    steps.append({"шаг": "контракт", "путь": str(papers["missing"]), "есть": False,
                  "почему": "бумаги нет"})
    return {"id": DETAIL_ID, "название": "Проба страницы задачи", "статус": "wip",
            "колонка": "ревью спеки",
            "пилюли": ["ждёт твоего слова по спеке, отправлена 13.10"],
            "уровень_объявлен": "П3", "уровень_по_коммитам": "П3", "цена": 8,
            "шаги": steps,
            "источники": [{"id": "inbox:1791905499", "текст": SOURCE_TEXT,
                           "строка": "inbox:1791905499"},
                          {"id": "ревизия:2026-10-01", "текст": None,
                           "строка": "ревизия:2026-10-01"}],
            "журнал": JOURNAL_TEXT,
            "коммиты": [{"хеш": COMMIT_HASH, "дата": "2026-10-13T22:10:00+03:00",
                         "тема": COMMIT_SUBJECT}],
            "скилы": [{"ts": "2026-10-13T20:00:00+03:00", "скил": SKILL_NAME,
                       "откуда_задача": "история"}]}


def board(papers: dict, taken: str) -> dict:
    tasks = [plain_task(i) for i in range(TASK_COUNT - 2)]
    tasks.append(dict(plain_task(98), id="pr.xss", название=XSS_TITLE,
                      колонка="очередь"))
    tasks.append(detail_task(papers))
    return {"снято": taken, "направление": "harness", "архив": 11,
            "колонки": COLUMNS, "задачи": tasks, "беда": None}


def write_board(path: Path, data) -> None:
    path.write_text(data if isinstance(data, str) else
                    json.dumps(data, ensure_ascii=False), encoding="utf-8")


def files_under(directory: Path) -> list:
    if not directory.exists():
        return []
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*"))


def mode(path: Path) -> str:
    try:
        return oct(stat.S_IMODE(path.stat().st_mode))
    except OSError as error:
        return f"нет: {error}"


# Юнит службы UMask не задаёт — значит 022. Проверка прав каталогов обязана
# идти при ТОЙ ЖЕ маске, иначе 2770 «получится» от маски проверяющего.
os.umask(0o022)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    project = root / "проект"
    product = root / "продукт"
    outside = root / "снаружи"
    logs = root / "логи"
    panel_state = root / "панель"
    for directory in (project / "docs" / "specs", product, outside,
                      logs / "inbox" / "обработано", panel_state, root / "секреты"):
        directory.mkdir(parents=True, exist_ok=True)
    (root / "harness.conf").write_text(HARNESS_CONF_TEXT + f'PRODUCT_REPO="{product}"\n',
                                      encoding="utf-8")
    (root / "install.conf").write_text(
        f'TG_CHAT_ID="100000001"\nPROJECT_DIR="{project}"\nLOG_DIR="{logs}"\n',
        encoding="utf-8")

    # Бумаги задачи: обычная, симлинк наружу корня, FIFO, большая, каталог,
    # абсолютный путь вне корня, отсутствующая. Корень бумаг — PROJECT_DIR и
    # PRODUCT_REPO из конфигов пробы, а не место, где лежит код панели.
    papers = {
        "normal": project / "docs" / "specs" / "hr.proba.md",
        "symlink_out": project / "docs" / "specs" / "hr.proba-REVIEW.md",
        "fifo": project / "fifo-proba",
        "big": project / "bolshaja-bumaga.md",
        "directory": project / "docs",
        "outside_abs": outside / "sekret.txt",
        "missing": project / "net-takoj.md",
    }
    papers["normal"].write_text(PAPER_TEXT, encoding="utf-8")
    papers["outside_abs"].write_text(OUTSIDE_SECRET, encoding="utf-8")
    os.symlink(papers["outside_abs"], papers["symlink_out"])
    os.mkfifo(papers["fifo"])
    papers["big"].write_text("б" * (150 * 1024), encoding="utf-8")   # 300 КБ в байтах
    board_file = root / "board.json"
    write_board(board_file, board(papers, iso_minutes_ago(1)))

    os.environ.update(
        HARNESS_CONF=str(root / "harness.conf"),
        HARNESS_INSTALL_CONF=str(root / "install.conf"),
        LOG_DIR=str(logs),
        SECRETS_DIR=str(root / "секреты"),
        PANEL_STATE_DIR=str(panel_state),
        PANEL_BOARD=str(board_file),
        PROJECT_DIR=str(project),
        PRODUCT_REPO=str(product),
        # Прежняя /karta читала живую dev-map.yaml: увести её в пустоту, чтобы
        # проба ни при каком коде не смотрела в боевую карту.
        PANEL_DEVMAP=str(root / "нет-карты.yaml"),
    )

    port = free_port()
    server.Панель.log_message = lambda self, f, *a: sys.stderr.write((f % a) + "\n")
    service = server.собрать(порт=port)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    own = {"Origin": base, "Sec-Fetch-Site": "same-origin"}

    def reset_rates():
        """Лимит чтения 60/мин на адрес: проба делает больше запросов, чем живой
        человек, и без сброса её отказы были бы «слишком часто», а не по делу."""
        server.ЧАСТОТА_ЧТЕНИЯ.следы.clear()
        server.ЧАСТОТА_ВХОДА.следы.clear()

    def log_in() -> str | None:
        reset_rates()
        token = server.вход().выдать_токен("100000001")
        code, body = request(f"{base}/vhod", data={"t": token}, headers=own)
        return json.loads(body).get("пропуск") if code == 200 else None

    def csrf_for(pass_value: str) -> str:
        code, body = request(f"{base}/nastrojki",
                             headers=dict(own, Cookie=f"panel={pass_value}"))
        return json.loads(body).get("csrf", "") if code == 200 else ""

    # ── (а) доска ────────────────────────────────────────────────────────────

    # Без пропуска ни доска, ни страница задачи, ни бумага не отдаются: в
    # снимке слова владельца и пути бумаг, это не для гостя.
    reset_rates()
    for path in ("/karta", f"/karta/{DETAIL_ID}", f"/karta/{DETAIL_ID}/fajl?n=1"):
        code, body = raw_get(port, path)
        if code == 401:
            path_ok(f"БОЛЬНОЙ СЛУЧАЙ: без пропуска {path} → 401")
        else:
            path_bad(f"без пропуска {path} → {code} {body[:80]}")

    pass_a = log_in()
    if pass_a:
        path_ok("пропуск для пробы получен")
    else:
        path_bad("пропуска нет — дальше пробы без входа")
        service.shutdown()
        summary_and_exit()
    with_pass = dict(own, Cookie=f"panel={pass_a}")

    # Владелец: «все задачи … были на канбане». Прежний экран резал карту до
    # 40 карточек — задача 41-я и дальше для владельца не существовала.
    reset_rates()
    code, page = raw_get(port, "/karta", with_pass)
    shown = [i for i in range(TASK_COUNT - 2) if f"Карточка-проба {i:02d}" in page]
    if code == 200 and len(shown) == TASK_COUNT - 2 and "Проба страницы задачи" in page:
        path_ok(f"доска показывает все {TASK_COUNT} задач снимка, без обрезки 40")
    else:
        path_bad(f"доска показала {len(shown)} из {TASK_COUNT - 2} проб: {code} {page[:120]}")

    # Колонки идут в порядке СНИМКА, и каждая карточка стоит в своей колонке.
    # Своего списка колонок у панели быть не должно: второй список стадий
    # разошёлся бы с гейтом (спека §1).
    # Ищется ЗАГОЛОВОК колонки: имена шагов теперь стоят и на карточках, и
    # голое «план» нашлось бы в карточке раньше своей колонки.
    positions, cursor = [], 0
    for column in COLUMNS:
        found = page.find(f"<h3>{column} · ", cursor)
        positions.append(found)
        cursor = found + len(column) if found >= 0 else cursor
    in_order = all(p >= 0 for p in positions) and positions == sorted(positions)
    if in_order:
        path_ok("колонки на доске идут в порядке снимка")
    else:
        path_bad(f"порядок колонок не тот: {list(zip(COLUMNS, positions))}")
    misplaced = []
    if in_order:
        bounds = positions + [len(page)]
        for i in range(TASK_COUNT - 2):
            column_index = COLUMNS.index(FILLED_COLUMNS[i % len(FILLED_COLUMNS)])
            where = page.find(f"Карточка-проба {i:02d}")
            if not bounds[column_index] < where < bounds[column_index + 1]:
                misplaced.append(f"{i:02d}→{FILLED_COLUMNS[i % len(FILLED_COLUMNS)]}")
    if in_order and not misplaced:
        path_ok("каждая карточка стоит в колонке, названной снимком")
    else:
        path_bad(f"карточки не в своих колонках: {misplaced[:6]}")

    # Пустая колонка не пропадает: владелец должен видеть, что на шаге никого,
    # а не гадать, есть ли шаг вообще.
    empty_index = COLUMNS.index("мутации")
    segment = (page[positions[empty_index]:positions[empty_index + 1]]
               if in_order else "")
    if "пусто" in segment:
        path_ok("пустая колонка «мутации» нарисована словом «пусто»")
    else:
        path_bad(f"в пустой колонке нет «пусто»: {segment[:120]!r}")

    if re.search(r"архив\D{0,40}11", page, re.IGNORECASE):
        path_ok("архив назван числом")
    else:
        path_bad("числа архива на доске нет")

    # Больной случай 14.10: пройденный путь был виден только на странице
    # задачи — на доске одна пилюля. Шаги со своими отметками стоят на карточке.
    detail_at = page.find("Проба страницы задачи")
    detail_card = page[detail_at:page.find("</a>", detail_at)] if detail_at >= 0 else ""
    if all(mark in detail_card for mark in ("✓ спека", "○ тесты", "✗ контракт")):
        path_ok("шаги задачи с отметками видны на карточке доски")
    else:
        path_bad(f"шагов на карточке доски нет: {detail_card[:200]!r}")

    # Колонки по высоте содержимого: под короткой колонкой на телефоне
    # оставалось ~4000 px пустоты до низа самой длинной.
    if re.search(r"\.board\{[^}]*align-items:flex-start", page):
        path_ok("колонки доски по высоте содержимого (align-items:flex-start)")
    else:
        path_bad("колонки доски тянутся до самой длинной: нет align-items:flex-start")
    # Многострочная пилюля на карточке — прямоугольник со скруглением: с
    # радиусом 99px она становится эллипсом, и текст вылезает за контур (N-2).
    if re.search(r"\.board-card \.пилюля\{[^}]*border-radius:12px", page):
        path_ok("пилюля на карточке доски — скруглённый прямоугольник")
    else:
        path_bad("пилюля на карточке доски — эллипс (border-radius 99px)")
    # Длинная колонка листается внутри себя: иначе вся страница тянется до её
    # низа, и под остальными колонками та же пустота.
    if re.search(r"\.board-column\{[^}]*max-height:[^}]*overflow-y:auto", page):
        path_ok("длинная колонка листается внутри себя, страница не тянется")
    else:
        path_bad("у колонки нет потолка высоты с прокруткой")

    if "ждёт твоего слова по спеке" in page:
        path_ok("пилюля карточки из снимка видна на доске")
    else:
        path_bad("пилюли карточки на доске нет")

    # Название задачи — данные из карты, а доска едет в браузер владельца:
    # неэкранированное название стало бы чужим скриптом на его странице.
    if "<script>alert(" not in page and "&lt;script&gt;" in page:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: название задачи с <script> экранировано")
    else:
        path_bad("название задачи с <script> не показано экранированным")

    if code == 200 and shown and "доска устарела" not in page:
        path_ok("свежий снимок — без полосы «доска устарела»")
    else:
        path_bad(f"свежий снимок: {code}, полоса устаревания есть или доски нет")

    # Снимок старше 15 минут — демон build-board молчит, и доска врёт о
    # стадиях. Владелец обязан это видеть, а не читать старое как новое.
    write_board(board_file, board(papers, iso_minutes_ago(20)))
    reset_rates()
    code, page_old = raw_get(port, "/karta", with_pass)
    if code == 200 and "доска устарела" in page_old:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: снимок старше 15 мин → «доска устарела»")
    else:
        path_bad(f"старый снимок без полосы: {code} {page_old[:120]}")

    # Битый снимок — беда словами, а не 500 и не пустая доска «задач нет».
    write_board(board_file, '{"снято": "2026-10-14T10:00:00", "задачи": [')
    code, page_bad = raw_get(port, "/karta", with_pass)
    names_trouble = re.search(r"(не прочитан|битый|испорчен|не разобран|не читается)",
                              page_bad, re.IGNORECASE)
    names_board = re.search(r"(сним|доск|board)", page_bad, re.IGNORECASE)
    if code not in (0, 500) and "Traceback" not in page_bad and names_trouble and names_board:
        path_ok(f"БОЛЬНОЙ СЛУЧАЙ: битый board.json → беда словами ({code})")
    else:
        path_bad(f"битый снимок не назван бедой: {code} {page_bad[:160]}")

    # Снимка нет вовсе (демон ещё не отработал) — тоже беда словами.
    os.environ["PANEL_BOARD"] = str(root / "нет-снимка.json")
    code, page_none = raw_get(port, "/karta", with_pass)
    if code not in (0, 500) and "Traceback" not in page_none and \
            re.search(r"(сним|доск)", page_none, re.IGNORECASE) and \
            re.search(r"(нет|не прочитан|не найден)", page_none, re.IGNORECASE):
        path_ok(f"снимка нет → беда словами ({code})")
    else:
        path_bad(f"отсутствие снимка не названо: {code} {page_none[:160]}")
    os.environ["PANEL_BOARD"] = str(board_file)
    write_board(board_file, board(papers, iso_minutes_ago(1)))

    # ── (б) страница задачи ─────────────────────────────────────────────────

    reset_rates()
    code, task_page = raw_get(port, f"/karta/{DETAIL_ID}", with_pass)
    if code == 200 and "Проба страницы задачи" in task_page:
        path_ok("страница задачи открывается по id из снимка")
    else:
        path_bad(f"страница задачи не открылась: {code} {task_page[:120]}")

    # id сверяется с МНОЖЕСТВОМ id снимка после unquote, а не по символам:
    # закодированная буква — тот же id.
    code, _ = raw_get(port, "/karta/hr%2Eproba", with_pass)
    if code == 200:
        path_ok("id после unquote совпал с id снимка → 200")
    else:
        path_bad(f"закодированный, но верный id отвергнут: {code}")

    # Обход каталога и чужой id — 404 (контракт: префиксный разбор пути отдал
    # бы чужое под видом задачи).
    for probe in ("/karta/../x", "/karta/..%2Fx", "/karta/hr.proba%2F..",
                  "/karta/%2Fetc%2Fpasswd", "/karta/net.takoj"):
        code, body = raw_get(port, probe, with_pass)
        if code == 404 and "Traceback" not in body:
            path_ok(f"БОЛЬНОЙ СЛУЧАЙ: {probe} → 404")
        else:
            path_bad(f"{probe} → {code} {body[:80]}")

    # Блоки страницы (спека §5): слова владельца, шаги, журнал, коммиты, скилы.
    blocks = {
        "слова владельца (текст inbox)": SOURCE_TEXT in task_page,
        "источник не из inbox — строкой как есть": "ревизия:2026-10-01" in task_page,
        "шаг есть — ✓": "✓" in task_page,
        "шаг без бумаги — ✗": "✗" in task_page,
        "шаг не судится — ○": "○" in task_page,
        "журнал задачи": JOURNAL_TEXT in task_page,
        "коммит: хеш и тема": COMMIT_HASH in task_page and COMMIT_SUBJECT in task_page,
        "скил из журнала вызовов": SKILL_NAME in task_page,
        "скил помечен «приписано по истории»": "приписано по истории" in task_page,
        "форма комментария со знаком записи":
            'action="/kommentarij"' in task_page and 'name="csrf"' in task_page,
    }
    for block, present in blocks.items():
        if present:
            path_ok(f"на странице задачи есть блок: {block}")
        else:
            path_bad(f"на странице задачи нет блока: {block}")

    # ── (в) просмотр бумаги ─────────────────────────────────────────────────
    # Номер — индекс в списке шагов СНИМКА, не путь. Нечётные номера попадают
    # в одну бумагу при любой базе счёта (см. detail_task).

    reset_rates()
    code, body = raw_get(port, f"/karta/{DETAIL_ID}/fajl?n=1", with_pass)
    if code == 200 and "&lt;b&gt;спека пробы&lt;/b&gt;" in body and "<b>спека пробы</b>" not in body:
        path_ok("бумага задачи открыта, текст экранирован")
    else:
        path_bad(f"бумага не открыта или не экранирована: {code} {body[:160]}")

    for n in ("999", "-1", "abc", ""):
        code, body = raw_get(port, f"/karta/{DETAIL_ID}/fajl?n={n}", with_pass)
        if code in (403, 404) and OUTSIDE_SECRET not in body:
            path_ok(f"БОЛЬНОЙ СЛУЧАЙ: номер бумаги «{n}» вне списка → {code}")
        else:
            path_bad(f"номер «{n}» → {code} {body[:80]}")

    code, body = raw_get(port, "/karta/net.takoj/fajl?n=1", with_pass)
    if code == 404:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: бумага чужого id → 404")
    else:
        path_bad(f"бумага неизвестного id → {code} {body[:80]}")

    # Симлинк из корня наружу и абсолютный путь вне корня: через панель не
    # должен уйти ни /etc, ни секрет — realpath обязан лежать внутри корня.
    for n, what in (("3", "симлинк наружу"), ("11", "абсолютный путь вне корня")):
        code, body = raw_get(port, f"/karta/{DETAIL_ID}/fajl?n={n}", with_pass)
        if code == 403 and OUTSIDE_SECRET not in body:
            path_ok(f"БОЛЬНОЙ СЛУЧАЙ: {what} → 403, секрет не отдан")
        else:
            path_bad(f"{what} → {code} {body[:80]}")

    # FIFO: открыть его на чтение — повесить поток службы навсегда. Ответ
    # обязан прийти (таймаут клиента 10 с) и быть отказом.
    started = time.time()
    code, body = raw_get(port, f"/karta/{DETAIL_ID}/fajl?n=5", with_pass)
    if code == 403 and time.time() - started < 9:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: FIFO → 403 без зависания")
    else:
        path_bad(f"FIFO → {code} за {time.time() - started:.1f} с {body[:80]}")

    code, body = raw_get(port, f"/karta/{DETAIL_ID}/fajl?n=7", with_pass)
    if code == 413:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: бумага больше 256 КБ → 413")
    else:
        path_bad(f"большая бумага → {code} {body[:80]}")

    code, body = raw_get(port, f"/karta/{DETAIL_ID}/fajl?n=9", with_pass)
    if code == 403:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: каталог вместо бумаги → 403")
    else:
        path_bad(f"каталог → {code} {body[:80]}")

    # ── (г) комментарии ─────────────────────────────────────────────────────

    inbox = logs / "inbox"
    inbox_before = files_under(inbox)
    comments = panel_state / "comments"
    csrf_a = csrf_for(pass_a)

    # Без пропуска запись не принимается.
    code, body = request(f"{base}/kommentarij", headers=own,
                         data={"id": DETAIL_ID, "текст": "проба", "csrf": csrf_a})
    if code == 401:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: комментарий без пропуска → 401")
    else:
        path_bad(f"комментарий без пропуска → {code} {body[:80]}")

    # Без знака записи — 403: иначе чужой сайт отправит комментарий от имени
    # владельца его же пропуском (C6).
    code, body = request(f"{base}/kommentarij", headers=with_pass,
                         data={"id": DETAIL_ID, "текст": "проба без знака"})
    if code == 403:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: комментарий без csrf → 403")
    else:
        path_bad(f"комментарий без csrf → {code} {body[:80]}")

    # Запасная сессия: канал мёртв, доставить комментарий агенту некому —
    # маршрут «опасный» (спека §4).
    reset_rates()
    login_state = server.вход()
    reserve_code = login_state.завести_запасной_код()
    request(f"{base}/zapasnoj", form={"код": reserve_code})
    login_state.сдвинуть_заявку(-login.ЗАПАСНОЙ_ЖДЁТ_СЕК - 1)
    cookie = raw_post_headers(f"{base}/zapasnoj", {"код": reserve_code}).get("Set-Cookie", "")
    reserve_pass = cookie.partition(f"{server.ИМЯ_ПРОПУСКА}=")[2].split(";")[0]
    if reserve_pass and login_state.запасная(reserve_pass):
        reserve_headers = dict(own, Cookie=f"panel={reserve_pass}")
        code, body = request(f"{base}/kommentarij", headers=reserve_headers,
                             data={"id": DETAIL_ID, "текст": "проба запасной",
                                   "csrf": csrf_for(reserve_pass)})
        if code == 403:
            path_ok("БОЛЬНОЙ СЛУЧАЙ: запасная сессия комментарий не оставляет → 403")
        else:
            path_bad(f"запасная сессия оставила комментарий: {code} {body[:80]}")
    else:
        path_bad(f"запасная сессия не заведена: {cookie[:80]}")

    # Пусто и длиннее потолка канала (канал.отказ_с = 1800) — 422.
    for text, what in (("", "пустой"), ("   \n ", "из пробелов"),
                       ("я" * 1801, "длиннее потолка 1800")):
        code, body = request(f"{base}/kommentarij", headers=with_pass,
                             data={"id": DETAIL_ID, "текст": text, "csrf": csrf_a})
        if code == 422:
            path_ok(f"БОЛЬНОЙ СЛУЧАЙ: комментарий {what} → 422")
        else:
            path_bad(f"комментарий {what} → {code} {body[:80]}")

    # Браузер шлёт форму без скриптов (CSP): отказ ему — страницей, не JSON.
    code, body = request(f"{base}/kommentarij", headers=with_pass,
                         form={"id": DETAIL_ID, "текст": "", "csrf": csrf_a})
    if code == 422 and not body.lstrip().startswith("{"):
        path_ok("отказ форме браузера — страницей, а не сырым JSON")
    else:
        path_bad(f"отказ форме браузера: {code} {body[:80]}")

    # id не из снимка — 404: комментарий к несуществующей задаче некому читать.
    code, body = request(f"{base}/kommentarij", headers=with_pass,
                         data={"id": "net.takoj", "текст": "проба", "csrf": csrf_a})
    if code == 404:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: комментарий к id не из снимка → 404")
    else:
        path_bad(f"комментарий к чужому id → {code} {body[:80]}")

    written_by_refusals = [f for f in files_under(comments) if f.endswith((".json", ".jsonl"))]
    if not written_by_refusals:
        path_ok("отказанные комментарии ничего не записали")
    else:
        path_bad(f"отказ оставил файлы: {written_by_refusals}")

    # Норма и частота: своя сессия и свой адрес, чтобы счёт лимита начался с
    # нуля независимо от того, к чему реализация его привязала.
    pass_b = log_in()
    rate_headers = dict(own, Cookie=f"panel={pass_b}", **{"X-Real-IP": "10.0.0.77"})
    csrf_b = csrf_for(pass_b)
    first_text = "Проба комментария: <i>курсив</i> и «да»"
    accepted = []
    for i in range(5):
        text = first_text if i == 0 else f"проба комментария {i}"
        code, body = request(f"{base}/kommentarij", headers=rate_headers,
                             data={"id": DETAIL_ID, "текст": text, "csrf": csrf_b})
        accepted.append(code)
    if all(c in (200, 201, 202, 303) for c in accepted):
        path_ok("пять комментариев за минуту приняты")
    else:
        path_bad(f"нормальные комментарии не приняты: {accepted}")

    # Шестой за минуту — 429: панель не должна стать насосом пинков агенту.
    code, body = request(f"{base}/kommentarij", headers=rate_headers,
                         data={"id": DETAIL_ID, "текст": "шестой", "csrf": csrf_b})
    if code == 429:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: шестой комментарий за минуту → 429")
    else:
        path_bad(f"шестой комментарий за минуту → {code} {body[:80]}")

    # Норма — строка в comments/<id>.jsonl и заявка comments/new/<ts>.json.
    # Ровно пять: отказы (403/422/404/429) не пишут ничего.
    lines_file = comments / f"{DETAIL_ID}.jsonl"
    try:
        lines = [json.loads(s) for s in lines_file.read_text(encoding="utf-8").splitlines()
                 if s.strip()]
    except (OSError, ValueError):
        lines = []
    if len(lines) == 5 and lines[0].get("текст") == first_text:
        path_ok(f"в comments/{DETAIL_ID}.jsonl ровно 5 строк, текст дословно")
    else:
        path_bad(f"строк в comments/{DETAIL_ID}.jsonl: {len(lines)} (ждали 5) {lines[:2]}")

    requests_dir = comments / "new"
    claims = sorted(requests_dir.glob("*.json")) if requests_dir.exists() else []
    try:
        claim = json.loads(claims[0].read_text(encoding="utf-8")) if claims else {}
    except (OSError, ValueError):
        claim = {}
    if len(claims) == 5 and claim.get("id") == DETAIL_ID and claim.get("текст") == first_text:
        path_ok("в comments/new/ ровно 5 заявок, в заявке id и текст")
    else:
        path_bad(f"заявок {len(claims)} (ждали 5), первая: {claim}")

    # Права: каталоги создаёт панель (harness-panel), читает и отмечает агент
    # (группа). 2770 — группа наследуется, чужим ничего; файлы 660.
    for directory in (comments, requests_dir):
        if mode(directory) == "0o2770":
            path_ok(f"каталог {directory.name}/ создан с правами 2770")
        else:
            path_bad(f"права каталога {directory.name}/: {mode(directory)}")
    for file_, what in ((lines_file, f"{DETAIL_ID}.jsonl"),
                        (claims[0] if claims else requests_dir / "<ts>.json", "заявка")):
        if mode(file_) == "0o660":
            path_ok(f"файл «{what}» создан с правами 660")
        else:
            path_bad(f"права файла «{what}»: {mode(file_)}")

    # Главное правило спеки §4: комментарий НЕ попадает во входящие. Оттуда
    # ворота выката и одобрение спеки берут слова владельца — «да, выкатывай»
    # с панели стало бы согласием мимо канала.
    inbox_after = files_under(inbox)
    if inbox_after == inbox_before and accepted and accepted[0] in (200, 201, 202, 303):
        path_ok("БОЛЬНОЙ СЛУЧАЙ: принятый комментарий не положил во входящие ни одного файла")
    else:
        path_bad(f"входящие: появилось {sorted(set(inbox_after) - set(inbox_before))}, "
                 f"комментарий принят: {accepted[:1]}")
    if not (logs / "comments").exists():
        path_ok("комментарии не легли в журнал харнеса (LOG_DIR)")
    else:
        path_bad("комментарии записаны в LOG_DIR, а не в PANEL_STATE_DIR")

    # Страница задачи показывает комментарий (экранированным) и его состояние.
    reset_rates()
    code, task_page = raw_get(port, f"/karta/{DETAIL_ID}", with_pass)
    if "&lt;i&gt;курсив&lt;/i&gt;" in task_page and "<i>курсив</i>" not in task_page:
        path_ok("комментарий виден на странице задачи, экранирован")
    else:
        path_bad("комментария на странице задачи нет или он не экранирован")
    if "ждёт доставки" in task_page:
        path_ok("недоставленный комментарий помечен «ждёт доставки»")
    else:
        path_bad("состояния «ждёт доставки» на странице нет")

    # ── ревью кода hr.kanban: архив null, S-04, S-05, G-13 ─────────────────
    def post_comment(address: str, task_id: str, text: str):
        """Комментарий со своим адресом: лимит частоты у каждой пробы свой."""
        headers = dict(own, Cookie=f"panel={pass_b}", **{"X-Real-IP": address})
        return request(f"{base}/kommentarij", headers=headers,
                       data={"id": task_id, "текст": text, "csrf": csrf_b})

    def comment_lines() -> list:
        try:
            return lines_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    # «архив»: null — архив не прочитан. Это «—», а не 0 и не «None».
    with_null_archive = board(papers, iso_minutes_ago(1))
    with_null_archive["архив"] = None
    write_board(board_file, with_null_archive)
    reset_rates()
    code, page = raw_get(port, "/karta", with_pass)
    if code == 200 and re.search(r"архив\D{0,10}—", page) and "None" not in page:
        path_ok("БОЛЬНОЙ СЛУЧАЙ: архив не прочитан (null) → «архив: —»")
    else:
        path_bad(f"архив null: {code}, {re.findall(r'архив.{0,20}', page)[:2]}")

    # S-05: id из снимка — имя файла комментария. id с `..`/`/` увёл бы запись
    # из comments/ к ключам входа.
    hostile = board(papers, iso_minutes_ago(1))
    hostile["задачи"].append(dict(plain_task(97), id="../x", название="Проба id"))
    write_board(board_file, hostile)
    code, body = post_comment("10.0.1.1", "../x", "проба id")
    if code in (403, 404, 422) and not (panel_state / "x.jsonl").exists() \
            and not (root / "x.jsonl").exists():
        path_ok(f"БОЛЬНОЙ СЛУЧАЙ S-05: id «../x» не стал именем файла → {code}")
    else:
        path_bad(f"id «../x»: {code} {body[:80]}")
    write_board(board_file, board(papers, iso_minutes_ago(1)))

    # S-04: каталог comments/ общий с группой без sticky — агент может заменить
    # файл службы симлинком. Служба не пишет и не меняет права по нему.
    victim = panel_state / "проба-сессии.json"
    victim.write_text('{"сессии": 1}', encoding="utf-8")
    os.chmod(victim, 0o600)
    saved = lines_file.read_text(encoding="utf-8")
    lines_file.unlink()
    os.symlink(victim, lines_file)
    code, body = post_comment("10.0.1.2", DETAIL_ID, "проба симлинка")
    victim_intact = victim.read_text(encoding="utf-8") == '{"сессии": 1}' \
        and mode(victim) == "0o600"
    if code not in (200, 201, 202, 303) and victim_intact:
        path_ok(f"БОЛЬНОЙ СЛУЧАЙ S-04: симлинк вместо журнала комментариев → {code}, "
                f"чужой файл цел")
    else:
        path_bad(f"симлинк: {code}, чужой файл цел {victim_intact}")
    lines_file.unlink()

    os.mkfifo(lines_file)
    reset_rates()
    started = time.time()
    code, body = raw_get(port, f"/karta/{DETAIL_ID}", with_pass)
    if code == 200 and time.time() - started < 9:
        path_ok("БОЛЬНОЙ СЛУЧАЙ S-04: FIFO вместо журнала — страница задачи без зависания")
    else:
        path_bad(f"FIFO вместо журнала: {code} за {time.time() - started:.1f} с")
    lines_file.unlink()
    lines_file.write_text(saved, encoding="utf-8")
    os.chmod(lines_file, 0o660)

    # G-13: заявка не записалась — строки на странице быть не должно, иначе
    # она висит «ждёт доставки» навсегда, а доставлять нечего.
    before = comment_lines()
    os.chmod(requests_dir, 0o550)
    try:
        code, body = post_comment("10.0.1.3", DETAIL_ID, "проба без заявки")
    finally:
        os.chmod(requests_dir, 0o2770)
    if code == 500 and comment_lines() == before:
        path_ok("БОЛЬНОЙ СЛУЧАЙ G-13: заявка не записана → 500, строки комментария нет")
    else:
        path_bad(f"заявка не записана: {code}, строк было {len(before)}, "
                 f"стало {len(comment_lines())}")

    service.shutdown()

summary_and_exit()
