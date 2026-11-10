#!/usr/bin/env python3
"""Витрина состояния: что с харнесом прямо сейчас, без единой команды.

Ничего нового здесь не замеряется — всё это харнес уже собирает сам: метки
демонов, очередь входящих, суточный расход, итог ворот, карта задач. Витрина
только складывает готовое в один снимок.

Главное правило экрана: величина, которой НЕТ, называет причину, а не
показывает ноль. «Сторожей нет данных» и «сторожа не отмечались» — разные
вещи, и на экране владельца они не должны выглядеть одинаково
([[segment-state-three-answers]]).
"""
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings                                       # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent


def _лог() -> Path:
    return Path(os.environ.get("PANEL_STATE_DIR_LOGS") or settings.журнал_дир())


def _метки() -> Path:
    return Path(settings.конф.heartbeat_dir())


def _карта() -> Path:
    return Path(os.environ.get("PANEL_DEVMAP") or КОРЕНЬ / "dev-map.yaml")


def _минут(путь: Path) -> int:
    return int((time.time() - путь.stat().st_mtime) / 60)


# Через сколько минут замер перестаёт быть «текущим». ДАННЫЕ, а не ручка:
# порог считает код, владельцу его задавать незачем (спека §«Свежесть замера»).
# У каждого источника свой: метки демонов обновляются по расписанию, расход —
# ежечасно, ворота — когда их гоняют.
ПОТОЛКИ_ВОЗРАСТА = {
    "метка": 180,        # самый редкий демон ходит раз в сутки, но метку
                         # обновляет чаще; три часа молчания — уже вопрос
    "расход": 120,       # сборщик ходит ежечасно
    "ворота": 1440,      # ворота гоняются по делу, не по расписанию
}


def свежесть(минут, вид: str, потолок: int | None = None) -> dict:
    """Поля возраста для витрины: сам возраст, протух ли он и почему.

    Возраст без потолка владельцу ничего не говорит: «обновлено 240 минут
    назад» — это норма для ворот и тревога для метки демона. Пометку ставит
    код, а не читающий глазами.
    """
    if минут is None:
        return {"минут": None, "устарело": None,
                "пояснение": "возраст неизвестен: источника нет"}
    потолок = потолок or ПОТОЛКИ_ВОЗРАСТА.get(вид, 180)
    устарело = минут > потолок
    return {"минут": минут, "устарело": устарело,
            "пояснение": (f"устарело на {минут - потолок} мин "
                          f"(потолок {потолок})" if устарело
                          else f"свежее потолка {потолок} мин")}


def работа() -> dict:
    """Задача, которая идёт сейчас. Поля берутся те, что в карте ЕСТЬ."""
    try:
        import yaml
        # Карта — сотни КБ: чистый питонный разбор брал 7 из 12 с главной страницы.
        карта = yaml.load(_карта().read_text(encoding="utf-8"),
                          Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except Exception as беда:                       # noqa: BLE001 — витрина не падает
        return {"нет данных": f"карта не прочитана: {беда}"}
    задачи = []

    def обход(узел):
        if isinstance(узел, dict):
            if "id" in узел and "status" in узел:
                задачи.append(узел)
            for значение in узел.values():
                обход(значение)
        elif isinstance(узел, list):
            for элемент in узел:
                обход(элемент)

    обход(карта)
    # Какая задача идёт сейчас, решает ПРИБОР ОЧЕРЕДИ, а не витрина. Улика
    # 13.09.2026: владелец открыл панель и прочёл «задач в работе нет», пока
    # шла работа, — витрина считала работой только статус wip, а очередь
    # считает и поставленное в очередь, и начатое, и берёт верхнюю по весу.
    # Два прибора об одном факте расходятся молча.
    первая = _первая_в_очереди()
    if первая is None:
        return {"нет данных": "очередь пуста: задач в работе нет"}
    по_ид = {з.get("id"): з for з in задачи}
    задача = по_ид.get(первая["id"], {})
    return {"id": первая["id"], "почему": первая["почему"],
            "название": задача.get("title", ""),
            "начата": (задача.get("when") or {}).get("started"),
            "блокер": задача.get("blocked"),
            "всего задач": len(задачи)}


def _первая_в_очереди() -> dict | None:
    """Первая строка из scripts/work-queue.py --порядок или None.

    Зовём команду, а не повторяем её расчёт: веса, свежесть источника и
    активное направление живут там, и своя копия разъедется в первый же день.
    """
    корень = Path(__file__).resolve().parent.parent.parent
    прибор = корень / "scripts" / "work-queue.py"
    if not прибор.exists():
        return None
    try:
        готово = subprocess.run(
            [sys.executable, str(прибор), str(_карта()), "--порядок"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for строка in готово.stdout.splitlines():
        if строка.startswith("сейчас делаем: "):
            хвост = строка[len("сейчас делаем: "):]
            if хвост.strip() == "очередь пуста":
                return None
            ид, _, почему = хвост.partition(" — ")
            return {"id": ид.strip(), "почему": почему.strip()}
    return None


def _активное_направление() -> str:
    """Над каким направлением карты работаем. Данные, не догадка.

    Спрашиваем общий разбор настроек панели: своя регулярка тут была третьей
    копией одного и того же чтения (ревизия лаконичности 12.09.2026).
    """
    из_окружения = os.environ.get("HARNESS_ACTIVE_LINE")
    if из_окружения is not None:
        return из_окружения
    значение, _ = settings.значение("ACTIVE_LINE")
    return значение or ""


# ── доска: снимок, который строит агентская сторона (scripts/build-board.py) ──
# Стадию задачи панель НЕ вычисляет: её судит гейт пайплайна, и вторая
# реализация для экрана разошлась бы с ним (спека hr.kanban §1). Панель читает
# готовый снимок и только называет его возраст.

# Снимок снимается каждую минуту (system-jobs.yaml) и при коммите; 15 минут
# без снимка — демон молчит, а не опоздал на круг (спека §3.1).
BOARD_STALE_MINUTES = 15
PAPER_LIMIT_BYTES = 256 * 1024
COMMENTS_DIR_MODE = 0o2770     # группа harness-panel наследуется: агент читает заявки
COMMENT_FILE_MODE = 0o660
DELIVERED_JOURNAL = "panel-comments-delivered.jsonl"
# id задачи — имя файла комментария: без `/` и без точки в начале (`..`).
# То же правило стоит в scripts/comments-intake.py — приём судит заявку сам.
TASK_ID = re.compile(r"(?!\.)[\w.\-]{1,200}")
# Каталог comments/ общий с группой агента и без sticky: файл службы там можно
# подменить симлинком или FIFO. Открываем без перехода по ссылке и без
# ожидания читателя, тип проверяем по открытому дескриптору.
_NO_SURPRISES = os.O_NOFOLLOW | os.O_NONBLOCK


def _board_path() -> Path:
    return Path(os.environ.get("PANEL_BOARD") or settings.состояние_дир() / "board.json")


def _minutes_since(moment: str | None) -> int | None:
    """Возраст ISO-времени в минутах; время без пояса считается местным."""
    try:
        taken = datetime.datetime.fromisoformat(str(moment))
    except ValueError:
        return None
    now = datetime.datetime.now(taken.tzinfo) if taken.tzinfo else datetime.datetime.now()
    return int((now - taken).total_seconds() // 60)


def board() -> dict:
    """Снимок доски с возрастом. Нет снимка или он битый — «нет данных» с причиной:
    пустая доска выглядела бы как «задач нет»."""
    path = _board_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"нет данных": f"снимка доски нет: {path} не найден — демон "
                              f"build-board его ещё не снимал"}
    except (OSError, ValueError) as error:
        return {"нет данных": f"снимок доски не прочитан: {path} битый или "
                              f"недоступен ({error})"}
    if not isinstance(data, dict) or not isinstance(data.get("задачи"), list):
        return {"нет данных": f"снимок доски не прочитан: в {path} нет списка задач"}
    age = _minutes_since(data.get("снято"))
    return dict(data, возраст_минут=age,
                устарела=age is None or age > BOARD_STALE_MINUTES)


def find_task(snapshot: dict, task_id: str) -> dict | None:
    """Задача по id — сверкой с id снимка, а не разбором символов пути."""
    return next((task for task in snapshot.get("задачи") or []
                 if isinstance(task, dict) and task.get("id") == task_id), None)


def _paper_roots() -> list[str]:
    """Корни, внутри которых лежат бумаги: проект и репозиторий продукта."""
    roots = [settings.конф.конфиг().get("PROJECT_DIR"),
             settings.конф.конфиг(settings.конф.путь_harness_conf()).get("PRODUCT_REPO")]
    return [os.path.realpath(root) for root in roots if root]


def read_paper(task: dict, number: str) -> tuple[int, str]:
    """(код, текст или причина). Номер — индекс в шагах снимка, а не путь:
    путь из адреса превратил бы панель в читалку любого файла машины."""
    steps = task.get("шаги") or []
    if not re.fullmatch(r"[0-9]+", number) or int(number) >= len(steps) \
            or not steps[int(number)].get("путь"):
        return 404, f"у задачи нет бумаги под номером «{number}»"
    try:
        # O_NONBLOCK: FIFO, открытый на чтение без него, повесил бы поток службы.
        descriptor = os.open(steps[int(number)]["путь"], os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return 404, "файла бумаги нет"
    except OSError as error:
        return 403, f"бумага не открывается: {error.strerror}"
    try:
        # Корень судится по ОТКРЫТОМУ файлу: путь до открытия агент может
        # подменить симлинком между проверкой и open.
        real = os.readlink(f"/proc/self/fd/{descriptor}")
        if not any(os.path.commonpath([real, root]) == root for root in _paper_roots()):
            return 403, "бумага лежит вне каталога проекта и продукта — не открываю"
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return 403, "это не обычный файл — не открываю"
        if info.st_size > PAPER_LIMIT_BYTES:
            return 413, (f"бумага {info.st_size // 1024} КБ — больше потолка "
                         f"{PAPER_LIMIT_BYTES // 1024} КБ, в панели не показываю")
        return 200, os.read(descriptor, PAPER_LIMIT_BYTES).decode("utf-8", "replace")
    finally:
        os.close(descriptor)


def comment_limit() -> int | None:
    """Потолок длины комментария — тот же, что у сообщения в канал (данные)."""
    try:
        import yaml
        data = yaml.safe_load(_pipeline_path().read_text(encoding="utf-8"))
        return int(data["канал"]["отказ_с"])
    except Exception:                                # noqa: BLE001 — витрина не падает
        return None


def _comments_dir() -> Path:
    return settings.состояние_дир() / "comments"


def _open_shared_dir(name: str, parent: int | None = None) -> int:
    """Дескриптор каталога comments/ или new/; создаётся при первом комментарии.
    mkdir под маской 022 отнял бы у группы запись и не поставил бы setgid —
    права ставятся явно, и только своему, только что созданному каталогу."""
    try:
        os.mkdir(name, COMMENTS_DIR_MODE, dir_fd=parent)
        created = True
    except FileExistsError:
        created = False
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    if created:
        os.fchmod(descriptor, COMMENTS_DIR_MODE)
    return descriptor


def _write_shared(directory: int, name: str, data: str, flags: int) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | _NO_SURPRISES | flags,
                         COMMENT_FILE_MODE, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{name} — не обычный файл")
        os.fchmod(descriptor, COMMENT_FILE_MODE)
        os.write(descriptor, data.encode("utf-8"))
    finally:
        os.close(descriptor)


def add_comment(task_id: str, text: str) -> dict:
    """Строка в comments/<id>.jsonl и заявка comments/new/<ts>.json.

    Во входящие харнеса текст не кладётся НИКОГДА: оттуда ворота выката и
    одобрение спеки берут согласия, и «да, выкатывай» с панели стал бы
    согласием мимо канала (спека §4). Заявку доставляет агенту диспетчер.

    Порядок — заявка черновиком, строка, переименование заявки: строка без
    заявки висела бы «ждёт доставки» вечно, а черновик с точкой приём не
    читает. Отказ на любом шаге — исключение без следа на странице."""
    if not TASK_ID.fullmatch(task_id):
        raise ValueError(f"id «{task_id}» не годится в имя файла")
    record = {"ts": f"{time.time():.9f}", "id": task_id, "текст": text}
    line = json.dumps(record, ensure_ascii=False)
    draft, final = f".{record['ts']}.json", f"{record['ts']}.json"
    state_dir = os.open(settings.состояние_дир(), os.O_RDONLY | os.O_DIRECTORY)
    comments_dir = new_dir = None
    try:
        comments_dir = _open_shared_dir("comments", state_dir)
        new_dir = _open_shared_dir("new", comments_dir)
        _write_shared(new_dir, draft, line, os.O_EXCL)
        try:
            _write_shared(comments_dir, f"{task_id}.jsonl", line + "\n", os.O_APPEND)
        except OSError:
            os.unlink(draft, dir_fd=new_dir)
            raise
        os.rename(draft, final, src_dir_fd=new_dir, dst_dir_fd=new_dir)
    finally:
        for descriptor in (new_dir, comments_dir, state_dir):
            if descriptor is not None:
                os.close(descriptor)
    return record


def _delivered() -> dict:
    """{ts: строка журнала доставки} — исход и время; журнал пишет приём."""
    try:
        lines = (settings.журнал_дир() / DELIVERED_JOURNAL).read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return {}
    result = {}
    for line in lines:
        try:
            row = json.loads(line)
            result[str(row["ts"])] = row
        except (ValueError, KeyError, TypeError):
            continue
    return result


def _read_comment_lines(task_id: str) -> list:
    path = _comments_dir() / f"{task_id}.jsonl"
    try:
        descriptor = os.open(path, os.O_RDONLY | _NO_SURPRISES)
    except FileNotFoundError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{path} — не обычный файл")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    finally:
        os.close(descriptor)


def comments(task_id: str) -> dict:
    """Комментарии к задаче с исходом доставки."""
    if not TASK_ID.fullmatch(task_id):
        return {"нет данных": f"id «{task_id}» не годится в имя файла комментариев"}
    try:
        lines = _read_comment_lines(task_id)
    except OSError as error:
        return {"нет данных": f"комментарии не прочитаны: {error}"}
    delivered = _delivered()
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        rows.append(dict(row, доставка=delivered.get(str(row.get("ts")))))
    return {"строки": rows}


def сторожа() -> dict:
    """Метки демонов с возрастом. Пусто — так и говорим."""
    каталог = _метки()
    try:
        # Файлы с точки — служебные следы механизмов, а не метки демонов:
        # так их и пропускает обход в sentinel (guard_bash пишет .guard_last.*
        # с точки НАРОЧНО). Витрина этого соглашения не знала и показала
        # владельцу «Сторожа 508, устарели 497» при 22 настоящих метках
        # (его снимок экрана 13.09.2026).
        файлы = sorted(п for п in каталог.iterdir()
                       if п.is_file() and not п.name.startswith("."))
    except OSError as беда:
        return {"нет данных": f"каталог меток недоступен: {беда}"}
    if not файлы:
        return {"нет данных": f"в {каталог} ни одной метки — демоны не отмечались"}
    периоды = _периоды_демонов()
    строки = []
    for п in файлы:
        # Порог — период ЭТОГО демона ×2, как у heartbeat-watch: backup ходит
        # раз в сутки, memory-revision — раз в месяц, и общий порог в три часа
        # красил здоровых. Периода нет в таблице — остаётся общий (владелец
        # 13.09.2026 видел «устарели 12 из 19» при живых демонах).
        потолок = периоды.get(п.name)
        строки.append(dict({"имя": п.name},
                           **свежесть(_минут(п), "метка",
                                      потолок=потолок * 2 if потолок else None)))
    return {"строки": строки}


def _периоды_демонов() -> dict:
    """{имя демона: период в минутах} из HEARTBEAT_PERIODS.

    Та же таблица, по которой судит heartbeat-watch: у факта «демон молчит
    дольше положенного» один судья, и это его таблица, а не догадка витрины.
    """
    сырое = os.environ.get("HEARTBEAT_PERIODS")
    if not сырое:
        конф = Path(settings.конф.путь_harness_conf())
        try:
            текст = конф.read_text(encoding="utf-8")
        except OSError:
            return {}
        найдено = re.search(r'^HEARTBEAT_PERIODS="([^"]*)"', текст, re.M)
        сырое = найдено.group(1) if найдено else ""
    итог = {}
    for кусок in сырое.split():
        имя, _, период = кусок.rpartition(":")
        if имя and период.isdigit():
            итог[имя] = int(период)
    return итог


def канал() -> dict:
    очередь = list((_лог() / "inbox").glob("*.txt")) if (_лог() / "inbox").exists() else []
    обработано = _лог() / "inbox" / "обработано"
    последнее = None
    if обработано.exists():
        файлы = sorted(обработано.glob("*.txt"), key=lambda п: п.stat().st_mtime)
        if файлы:
            последнее = _минут(файлы[-1])
    return {"в очереди": len(очередь), "последнее сообщение, минут назад": последнее}


def расход() -> dict:
    """Числа суточного расхода — из машинного файла сборщика.

    Прежде витрина разбирала человеческий отчёт: искала значения по подписям
    и чистила их пятью .replace. Подпись — не контракт: её правка оставляла
    владельца без чисел молча (находка ревью 11.09.2026)."""
    файл = _лог() / "tokens-daily.json"
    try:
        числа = json.loads(файл.read_text(encoding="utf-8"))
    except (OSError, ValueError) as беда:
        return {"нет данных": f"машинный отчёт о расходе не прочитан: {беда}"}
    # Валидный JSON, который НЕ объект («"текст"», «[1,2]»), ронял dict() —
    # и с ним весь снимок панели, а не одну карточку (ревью 13.09.2026).
    if not isinstance(числа, dict):
        return {"нет данных": "машинный отчёт о расходе не словарь чисел"}
    return dict(числа, **{"обновлён минут назад": _минут(файл)},
                **свежесть(_минут(файл), "расход"))


def проверки() -> dict:
    """Итог последнего прогона ворот — его пишут сами ворота."""
    журнал = _лог() / "ворота-итог.jsonl"
    try:
        строки = [с for с in журнал.read_text(encoding="utf-8").splitlines() if с.strip()]
    except OSError:
        return {"нет данных": "ворота ещё не писали итога — прогона не было"}
    if not строки:
        return {"нет данных": "журнал ворот пуст"}
    try:
        запись = json.loads(строки[-1])
    except ValueError:
        return {"нет данных": "последняя запись журнала ворот не разбирается"}
    запись["минут назад"] = _минут(журнал)
    запись.update(свежесть(_минут(журнал), "ворота"))
    return запись


def машина() -> dict:
    всего, занято, _ = shutil.disk_usage("/")
    итог = {"диск занято %": round(занято / всего * 100)}
    try:
        готово = subprocess.run(["systemctl", "is-system-running"],
                                capture_output=True, text=True, timeout=10)
        итог["системные службы"] = готово.stdout.strip() or "неизвестно"
    except (OSError, subprocess.SubprocessError):
        итог["системные службы"] = "спросить не удалось"
    return итог


# Что делает каждый шаг — словами человека без подготовки. Владелец:
# «пояснения нужны ко всему очень простые доступные чтобы бабушка поняла».
ПОЯСНЕНИЯ_ШАГОВ = {
    "спека": "описать словами, что и зачем делаем, — до всякого кода",
    "ревью_спеки": "дать чужим глазам поискать дыры в этом описании",
    "контракт": "договориться заранее, чем будем проверять, что готово",
    "план": "разложить работу на шаги с готовыми командами",
    "ревью_кода": "перечитать написанное по ролям перед сохранением",
    "тесты": "проверки пишутся до кода, а не после",
    "мутации": "нарочно сломать копию механизма и убедиться, что проверка это заметит",
    "безопасность": "проверить правку на дыры: вход, секреты, права, зависимости, контейнеры",
}


def _pipeline_path() -> Path:
    return Path(os.environ.get("HARNESS_PAJPLAJN")
                or КОРЕНЬ / "harness" / "config" / "pipeline.yaml")


def шаги() -> dict:
    """Шаги работы по уровням задачи — из пульта владельца."""
    try:
        import yaml
        данные = yaml.safe_load(_pipeline_path().read_text(encoding="utf-8"))
        по_уровням = данные["шаги"]
    except Exception as беда:                       # noqa: BLE001
        return {"нет данных": f"пульт шагов не прочитан: {беда}"}
    все = list(ПОЯСНЕНИЯ_ШАГОВ)
    return {"уровни": {у: [ш for ш in (по_уровням.get(у) or [])]
                       for у in ("мелкая", "обычная", "крупная")},
            "все шаги": все, "поясняет": ПОЯСНЕНИЯ_ШАГОВ}


def _пакет_по_паспорту() -> str:
    """Каталог пакета по конвенции установки: /home/<AGENT_USER>/starter/…

    Паспорт читает ОБЩАЯ функция (scripts/lib/config.py): рукописная копия этой
    выборки жила здесь и ещё в трёх местах, и копии расходились в ответе —
    одни брали первое совпадение, другие последнее (ревью кода 12.09.2026, №7).
    """
    sys.path.insert(0, str(КОРЕНЬ / "scripts" / "lib"))
    from config import из_файла
    агент = из_файла(settings.конф.путь_install_conf()).get("AGENT_USER", "")
    return f"/home/{агент or 'agent'}/starter/STARTER-PACKAGE"


def память(искать: str = "") -> dict:
    """Записи памяти проекта: имя, тема, размер, входит ли в набор установки."""
    каталог = Path(os.environ.get("HARNESS_PAMYAT") or КОРЕНЬ / "память")
    пакет = Path(os.environ.get("HARNESS_STARTER")
                 or _пакет_по_паспорту()) / "harness" / "память"
    try:
        файлы = sorted(каталог.glob("*.md"))
    except OSError as беда:
        return {"нет данных": f"память не прочитана: {беда}"}
    # Пакет лежит в домашнем каталоге, а служба панели живёт с ProtectHome —
    # заглянуть туда она не может и не должна. Набор она читает СПИСКОМ из
    # репозитория; пока список не заведён, признак честно неизвестен.
    список_набора = Path(os.environ.get("HARNESS_NABOR")
                         or КОРЕНЬ / "harness" / "config" / "memory-set.txt")
    try:
        в_наборе = {с.strip() for с in список_набора.read_text(encoding="utf-8").splitlines()
                    if с.strip() and not с.startswith("#")}
    except OSError:
        try:
            в_наборе = {п.name for п in пакет.glob("*.md")}
        except OSError:
            в_наборе = set()
    строки = []
    искать = искать.strip().lower()
    for файл in файлы:
        try:
            текст = файл.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        тема = ""
        for строка in текст.splitlines()[:8]:
            if строка.startswith("description:"):
                тема = строка.partition(":")[2].strip()
                break
        if искать and искать not in текст.lower() and искать not in файл.name.lower():
            continue
        строки.append({"имя": файл.name, "тема": тема, "знаков": len(текст),
                       "в наборе": файл.name in в_наборе})
    return {"строки": строки, "всего": len(файлы), "в наборе": len(в_наборе)}


def запись(имя: str) -> dict:
    """Одна запись памяти целиком — для правки."""
    каталог = Path(os.environ.get("HARNESS_PAMYAT") or КОРЕНЬ / "память")
    файл = каталог / имя
    if файл.resolve().parent != каталог.resolve() or not файл.exists():
        return {"нет данных": f"записи «{имя}» нет"}
    return {"имя": имя, "текст": файл.read_text(encoding="utf-8", errors="replace")}


def устройство(искать: str = "") -> dict:
    """Из чего состоит харнес: тот же реестр, из которого собран документ.

    Панель и документ не имеют права разойтись, поэтому источник один —
    harness/config/mechanisms.yaml, а не вторая копия описаний в коде экрана.
    """
    файл = Path(os.environ.get("HARNESS_USTROJSTVO")
                or КОРЕНЬ / "harness" / "config" / "mechanisms.yaml")
    import yaml
    try:
        записи = yaml.safe_load(файл.read_text(encoding="utf-8"))["механизмы"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as беда:
        return {"нет данных": f"описание устройства не прочитано: {беда}"}
    искать = искать.strip().lower()
    группы = {}
    найдено = 0
    try:
        return _разобрать_устройство(записи, искать, группы)
    except (KeyError, TypeError, AttributeError) as беда:
        # Запись без обязательного поля — это не повод ронять весь экран
        # (F-gen-02): страница обязана назвать беду, а не закрыть соединение.
        return {"нет данных": f"описание устройства неполно: {беда}"}


def _носитель_в_вид() -> dict:
    """Перевод носителя в вид — из СХЕМЫ, а не из своего списка в экране.

    Схема знает девять носителей, экран — четыре группы. Механизм с носителем
    «служба» или «сборщик» без перевода попал бы в счётчик «всего», но не попал
    бы ни в одну группу: исчез бы с экрана владельца при верном числе. Своя
    копия перевода разошлась бы со схемой молча — потому он один.
    """
    файл = Path(os.environ.get("HARNESS_PASPORT")
                or КОРЕНЬ / "harness" / "config" / "passport.yaml")
    import yaml
    try:
        return yaml.safe_load(файл.read_text(encoding="utf-8"))["носитель_в_вид"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return {}


def _вид_записи(запись: dict, перевод: dict) -> str:
    """Группа экрана для записи: вид как есть, носитель — переводом.

    Носитель приходит двумя путями: отдельным полем и в самом поле «вид» (на
    переходе к реестру v2 паспорт несёт носитель там же). Поэтому переводится
    ЛЮБОЕ значение, которое перевод знает, а незнакомое остаётся как есть —
    группой «прочее» оно станет ниже, но с экрана не пропадёт.
    """
    for ключ in ("вид", "носитель"):
        значение = str(запись.get(ключ) or "").strip()
        if значение in перевод:
            return перевод[значение]
        if значение and ключ == "вид":
            return значение
    return ""


def _разобрать_устройство(записи: list, искать: str, группы: dict) -> dict:
    найдено = 0
    перевод = _носитель_в_вид()
    for запись_ in записи:
        если_искали = " ".join(str(з) for з in запись_.values()).lower()
        if искать and искать not in если_искали:
            continue
        найдено += 1
        группы.setdefault(_вид_записи(запись_, перевод) or "прочее", []).append({
            "имя": запись_["имя"], "файл": запись_["файл"],
            "роль": " ".join(str(запись_["роль"]).split()),
            "когда": " ".join(str(запись_["когда"]).split()),
            "без него": " ".join(str(запись_["без_него"]).split()),
            "проверяется": " ".join(str(запись_["проверяется"]).split()),
            "пишет": " ".join(str(запись_["пишет"]).split()),
        })
    for строки in группы.values():
        строки.sort(key=lambda с: с["имя"])
    # Порядок задаёт вид показа, но НЕ решает, кого показать: группа вне
    # порядка идёт следом, а не пропадает. Прежний код молча ронял всё, чего
    # не было в кортеже, — и счётчик при этом оставался верным.
    порядок = ("демон", "хук", "гейт", "скил")
    хвост = [вид for вид in группы if вид not in порядок]
    return {"всего": len(записи), "найдено": найдено,
            "группы": [{"вид": вид, "строки": группы[вид]}
                       for вид in (*порядок, *sorted(хвост)) if вид in группы]}


# ───────────────────────── раздел «Процессы» ─────────────────────────

BACKGROUND_KEY = "BACKGROUND_PROCESSES"
CHECKS_SLICE_DIR = "/sys/fs/cgroup/harness.slice/harness-checks.slice"
VIEW_FILE = "process-dispatcher-view.json"
CHECKS_RUNS_SHOWN = 5
RUNS_TAIL_BYTES = 2_000_000


def _process_library_module():
    """Модуль библиотеки процессов: в проекте scripts/lib, в пакете harness/scripts/lib."""
    for folder in Path(__file__).resolve().parents:
        if (folder / "scripts" / "lib" / "process_library.py").is_file():
            if str(folder / "scripts" / "lib") not in sys.path:
                sys.path.insert(0, str(folder / "scripts" / "lib"))
            break
    import process_library
    return process_library


def _short_reason(error: Exception) -> str:
    """Класс сбоя и причина без путей: полный текст и путь остаются в журнале панели."""
    reason = re.sub(r"(?:/[\w.@+-]+)+", "…", str(error))[:120]
    return f"{type(error).__name__}: {reason}" if reason else type(error).__name__


def _section(build) -> dict:
    """Сбой одного блока страницы называет причину и не роняет остальные."""
    try:
        return build()
    except Exception as error:                      # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return {"нет данных": _short_reason(error)}


def _background_state() -> str:
    """on / off, как их читает обёртка run-background.sh: пустой или нечитаемый конфиг — off."""
    values = settings.конф.из_файла(settings.конф.путь_harness_conf())
    return "on" if values and values.get(BACKGROUND_KEY, "on").strip() == "on" else "off"


def _mechanisms_by_file() -> dict:
    """файл → запись механизма: «что делает» процесса без своего «зачем» берётся оттуда."""
    path = Path(os.environ.get("HARNESS_USTROJSTVO")
                or КОРЕНЬ / "harness" / "config" / "mechanisms.yaml")
    import yaml
    try:
        records = yaml.safe_load(path.read_text(encoding="utf-8"))["механизмы"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return {}
    return {record["файл"]: record for record in records if isinstance(record, dict) and record.get("файл")}


def _words(value) -> str:
    return " ".join(str(value or "").split())


def _process_state(record: dict, exempt: bool, key: str) -> str:
    """идёт · погашен · вне · снят · иное: что выключатель делает с этой записью сейчас."""
    status = record.get("статус", "")
    if status.startswith("снят"):
        return "снят"
    if status != "работает":
        return "иное"
    if record["класс"] != "фоновый" or record["запуск"] == "служба-проверок":
        return "вне"
    return "идёт" if exempt or key == "on" else "погашен"


def _process_card(record: dict, schedule: dict, exemptions: dict, mechanisms: dict, key: str, words) -> dict:
    mechanism = next((mechanisms[путь] for путь in [*(record.get("механизмы") or []), *(record.get("файлы") or [])]
                      if путь in mechanisms), {})
    exempt = record["id"] in exemptions
    return {
        "id": record["id"], "название": record["название"], "класс": record["класс"],
        "запуск": record["запуск"], "статус": record.get("статус", ""),
        "состояние": _process_state(record, exempt, key),
        "исключение": exemptions.get(record["id"]),
        "расписание": words((schedule.get(record["id"]) or {}).get("расписание"), record["запуск"]),
        "что_делает": _words(record.get("зачем") or mechanism.get("роль")),
        "шаги": [_words(step) for step in record.get("что_делает") or []],
        "когда": _words(mechanism.get("когда")), "без_него": _words(mechanism.get("без_него")),
        "файлы": list(record.get("файлы") or []),
        "замер": (record.get("доля_нагрузки") or {}).get("замер"),
    }


def _process_library() -> dict:
    library = _process_library_module()
    records = library.registry()["процессы"]
    schedule = library.load()
    exemptions = {record["id"]: _words(record.get("основание"))
                  for record in (schedule.get("выключатель") or {}).get("исключения") or []}
    mechanisms, key = _mechanisms_by_file(), _background_state()
    class_order = {"фоновый": 0, "по внутреннему действию": 1, "сторонний": 2}
    cards = [_process_card(r, schedule.get("процессы") or {}, exemptions, mechanisms, key, library.schedule_words)
             for r in records]
    cards.sort(key=lambda c: (class_order.get(c["класс"], 3), c["название"].lower()))
    return {"ключ": key, "исключений": sum(1 for c in cards if c["исключение"]), "записи": cards}


def _dispatcher_view() -> dict:
    path = _лог() / VIEW_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"нет данных": "Данных пока нет: диспетчер ещё не собрал первый замер"}
    try:
        view = json.loads(text)
    except ValueError as error:
        return {"нет данных": f"файл витрины диспетчера не разобран: {error}"}
    if not isinstance(view, dict) or not isinstance(view.get("окна"), dict):
        return {"нет данных": "файл витрины диспетчера неполон: нет окон"}
    return view


def _systemctl(*args: str) -> str:
    program = os.environ.get("PANEL_SYSTEMCTL") or "systemctl"
    done = subprocess.run([program, *args], capture_output=True, text=True, timeout=10, check=True)
    return done.stdout


def _unit_properties(units: list, props: str) -> dict:
    """{юнит: {свойство: значение}}: блоки ответа `systemctl show` идут в порядке юнитов."""
    blocks = _systemctl("show", *units, "-p", props).strip().split("\n\n")
    return {unit: dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            for unit, block in zip(units, blocks)}


def _cgroup_value(name: str):
    try:
        return (Path(os.environ.get("PANEL_CGROUP_CHECKS") or CHECKS_SLICE_DIR) / name).read_text().strip()
    except OSError:
        return None


def _number(text):
    """Число из значения cgroup/systemctl; «max», «infinity», «[not set]» и пусто — None (нет потолка / нет значения)."""
    return int(text) if text and text.strip().isdigit() else None


def _checks_slice(show: dict) -> dict:
    """Что действует СЕЙЧАС: файлы cgroup, а где их нет — `systemctl show`. Конфиг не читается:
    задуманное и действующее могут разойтись, и показывать надо второе."""
    quota = (_cgroup_value("cpu.max") or "").split()
    if quota and quota[0].isdigit() and len(quota) == 2 and quota[1].isdigit():
        cores = int(quota[0]) / int(quota[1])
    else:
        usec = (show.get("CPUQuotaPerSecUSec") or "").removesuffix("s")
        try:
            cores = float(usec)
        except ValueError:
            cores = None
    cpu_stat = dict(line.split() for line in (_cgroup_value("cpu.stat") or "").splitlines() if len(line.split()) == 2)

    def memory(name: str, prop: str):
        return _number(_cgroup_value(name)) if _cgroup_value(name) else _number(show.get(prop))
    return {"ядер_потолок": cores, "ядер_всего": os.cpu_count(),
            "память_мягкий": memory("memory.high", "MemoryHigh"), "память_жёсткий": memory("memory.max", "MemoryMax"),
            "память_сейчас": memory("memory.current", "MemoryCurrent"), "память_пик": _number(_cgroup_value("memory.peak")),
            "торможений": _number(cpu_stat.get("nr_throttled"))}


def _log_tail(path: Path) -> list:
    """Строки хвоста файла (до RUNS_TAIL_BYTES): журнал растёт до десятков МБ, а страницу открывают каждую минуту."""
    try:
        with open(path, "rb") as log:
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - RUNS_TAIL_BYTES))
            lines = log.read().splitlines()
    except FileNotFoundError:
        return []
    return lines[1:] if size > RUNS_TAIL_BYTES else lines      # первая строка хвоста может быть оборвана


def _check_runs(ids: list) -> dict:
    """{id: [последние прогоны, новые первыми]} из process-runs.jsonl: конец прогона с кодом и временем."""
    from collections import deque
    runs = {i: deque(maxlen=CHECKS_RUNS_SHOWN) for i in ids}
    for line in _log_tail(_лог() / "process-runs.jsonl"):
        if b'"process.end"' not in line:
            continue
        try:
            event = json.loads(line)
            data = event["data"]
            moment = datetime.datetime.fromisoformat(event["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if data.get("id") in runs:
            runs[data["id"]].appendleft({"ts": moment, "с": data.get("с"), "rc": data.get("rc"),
                                         "cpu_с": data.get("cpu_с")})
    return {i: list(v) for i, v in runs.items()}


def _running_since(show: dict):
    """«чч:мм» начала прогона, если служба идёт (oneshot в работе — activating), иначе None."""
    if show.get("ActiveState") not in ("active", "activating"):
        return None
    found = re.search(r"(\d\d:\d\d):\d\d", show.get("ExecMainStartTimestamp") or "")
    return found.group(1) if found else "?"


def _checks_service() -> dict:
    library = _process_library_module()
    records = {r["id"]: r for r in library.registry()["процессы"]}
    ids = library.checks_ids()
    schedule = library.load().get("процессы") or {}
    units = ["harness-checks.slice"] + [f"harness-{i}.{kind}" for i in ids for kind in ("timer", "service")]
    show = _unit_properties(units, "LoadState,ActiveState,UnitFileState,ExecMainStartTimestamp,"
                                   "CPUQuotaPerSecUSec,MemoryHigh,MemoryMax,MemoryCurrent")
    if not any(show[f"harness-{i}.timer"].get("LoadState") == "loaded" for i in ids):
        return {"нет данных": "Служба проверок ещё не установлена"}
    next_run = {entry["unit"]: entry.get("next") for entry in json.loads(_systemctl("list-timers", "harness-*", "--all",
                                                                       "--output=json") or "[]")}
    runs = _check_runs(ids)
    rows = []
    for i in ids:
        timer, service = show[f"harness-{i}.timer"], show[f"harness-{i}.service"]
        upcoming = next_run.get(f"harness-{i}.timer")
        rows.append({"id": i, "название": records[i]["название"],
                     "расписание": library.schedule_words((schedule.get(i) or {}).get("расписание"), records[i]["запуск"]),
                     "юнит": f"harness-{i}.timer", "включён": timer.get("ActiveState") == "active",
                     "идёт_с": _running_since(service),
                     "следующий": upcoming / 1_000_000 if upcoming else None, "прогоны": runs[i]})
    return {"срез": _checks_slice(show["harness-checks.slice"]), "таймеры": rows}


def processes() -> dict:
    """Раздел «Процессы»: библиотека, диспетчер (из витрины), служба проверок.
    У каждого блока своё «нет данных» — сбой одного остальные не трогает."""
    library = _section(_process_library)
    return {"библиотека": library, "диспетчер": _section(_dispatcher_view), "проверки": _section(_checks_service),
            "названия": {c["id"]: c["название"] for c in library.get("записи", [])},
            "ядер": os.cpu_count(), "снято": time.time()}


def обновления() -> dict:
    """Что с версией харнеса. Каждое состояние называется словами (§4 спеки).

    Панель НЕ ходит в сеть сама: она читает то, что оставила суточная сверка,
    и честно говорит, когда сверки ещё не было — молчание и «всё свежо» на
    экране обязаны различаться.
    """
    версия = {}
    файл = Path(settings.конф.version_file())
    try:
        версия = json.loads(файл.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        версия = {}
    журнал = _лог() / "update-watch.log"
    try:
        строки = [с for с in журнал.read_text(encoding="utf-8",
                                              errors="replace").splitlines() if с.strip()]
    except OSError:
        строки = []
    if not строки:
        return {"нет данных": "проверок ещё не было", "версия": версия}
    последняя = строки[-1]
    состояние = "стоит последняя версия"
    if "есть новая версия" in последняя:
        состояние = "есть новая версия"
    elif "не удалось" in последняя:
        состояние = "спросить не удалось"
    elif "откат" in последняя.lower():
        состояние = "обновление откатилось"
    return {"состояние": состояние, "строка": последняя[-160:],
            "версия": версия, "проверено минут назад": _минут(журнал)}


def снять() -> dict:
    """Полный снимок для экрана."""
    return {"работа": работа(), "сторожа": сторожа(), "канал": канал(),
            "обновления": обновления(),
            "шаги": шаги(),
            "расход": расход(), "проверки": проверки(), "машина": машина(),
            "снято": time.strftime("%H:%M:%S")}


if __name__ == "__main__":
    print(json.dumps(снять(), ensure_ascii=False, indent=2))
