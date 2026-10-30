#!/usr/bin/env python3
"""Границы карточки задачи в карте разработки — один разбор на всех писателей.

СЛУЧАЙ. Карту правят шесть механизмов, и границы блока карточки заново писал
каждый (ревизия архитектуры 12.09.2026). Класс дефекта чинится в ОДНОЙ копии:
12.09 архиватор перевёл отступ на класс `[ \\t]`, а приёмка осталась на `\\s`,
который включает перевод строки, — то есть могла закрыть соседнюю карточку.
Две аварии этого класса за четыре дня: 88 задач 09.09, 15 задач 12.09.

Почему текстом, а не через YAML. Карта правится ХИРУРГИЧЕСКИ: yaml.dump
переписал бы весь файл — порядок ключей, комментарии, форматирование, — и
дифф задачи стал бы диффом всей карты.

    python3 scripts/lib/cards.py --selftest
    python3 scripts/lib/cards.py --границы <id> <файл>   # для bash-писателей
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

# Отступ считается КЛАССОМ [ \\t], а не \\s: `\\s` включает перевод строки, и у
# карточки, перед которой стоит пустая строка, «отступ» выходил на единицу
# больше. Улика 12.09.2026: условие «следующий отступ ≤ моего» не сработало ни
# разу, и блок карточки поглотил ОДИННАДЦАТЬ соседних задач.
НАЧАЛО = re.compile(r"^([ \t]+)- id:\s*(\S+)\s*$", re.M)
# Строка, которая кончает список задач: что-то на отступе МЕНЬШЕ, чем у «- id:».
МЕНЬШИЙ_ОТСТУП = re.compile(r"^([ \t]*)\S", re.M)


def блоки(текст: str) -> list[tuple[str, str, int, int]]:
    """[(id, текст блока, начало, конец)] по всем задачам карты.

    Конец блока — следующая задача ТОГО ЖЕ уровня ЛИБО первая строка с меньшим
    отступом: конец списка задач, начало новой секции файла. «До конца файла»
    нельзя: блок последней задачи утаскивал всё, что стоит ниже, и карта
    оставалась валидным YAML — то есть пропажу не поймал бы никто.
    """
    итог = []
    совпадения = list(НАЧАЛО.finditer(текст))
    for i, совпало in enumerate(совпадения):
        отступ = len(совпало.group(1))
        начало = совпало.start()
        конец = len(текст)
        for следующее in совпадения[i + 1:]:
            if len(следующее.group(1)) <= отступ:
                конец = следующее.start()
                break
        for строка in МЕНЬШИЙ_ОТСТУП.finditer(текст, совпало.end()):
            if len(строка.group(1)) < отступ:
                конец = min(конец, строка.start())
                break
        итог.append((совпало.group(2), текст[начало:конец], начало, конец))
    return итог


def блок(текст: str, ид: str) -> tuple[str, int, int] | None:
    """(текст блока, начало, конец) одной карточки либо None."""
    for найден, тело, начало, конец in блоки(текст):
        if найден == ид:
            return тело, начало, конец
    return None


def поле(блок_текста: str, имя: str) -> str | None:
    """Значение поля верхнего уровня карточки: строкой, без кавычек."""
    найдено = re.search(rf"^\s+{re.escape(имя)}:\s*(.*)$", блок_текста, re.M)
    if not найдено:
        return None
    значение = найдено.group(1).strip()
    if значение[:1] in "\"'" and значение[-1:] == значение[:1]:
        значение = значение[1:-1]
    return значение


# ── общий писатель карточек и состояние тревог ─────────────────────────────
# Карту пишут map-from-message.py и alert-task.py. Лок, вставка перед якорем и
# сверка «ни один id не пропал, прибавилось ровно новых» живут ЗДЕСЬ в одной
# копии: вторая копия сверки расходится молча (улика 09.09.2026: 88 задач).

CLOSED_STATUSES = ("done", "принято")
DEV_MAP_TRAILER = re.compile(r"^Dev-Map:(.*)$", re.M)


def dev_map_ids(text: str) -> list[str]:
    """id из строк сообщения коммита, что начинаются с «Dev-Map:»; порядок появления, без повторов.

    Единственный разбор трейлера: суд пайплайна, приёмка и доска обязаны видеть
    один и тот же набор задач, иначе коммит «чужой» для одного и «свой» для другого.
    """
    found = (task_id for line in DEV_MAP_TRAILER.findall(text)
             for task_id in re.split(r"[,\s]+", line) if task_id)
    return list(dict.fromkeys(found))
JOURNAL_NAME = "тревоги-сторожей.jsonl"
QUEUE_NAME = "тревоги-ждут.jsonl"
ALERT_SOURCE_PREFIX = "тревога:"
# События журнала, после которых ключ считается открытым / зелёным.
OPEN_EVENTS = ("заведена", "донесена", "повтор")
BIRTH_EVENTS = ("заведена", "донесена")
GREEN_EVENT = "зелёное"
# Статусы, в которых карточка тревоги работой модели не считается: test ждёт
# проверки, беклог — слово владельца «не трогать».
NOT_WEIGHTED_STATUSES = CLOSED_STATUSES + ("test", "беклог")


class LockBusy(Exception):
    """Лок карты не взят за отведённое время. Не отказ: намерение можно отложить."""


class MapRefused(SystemExit):
    """Сверка под локом не сошлась. SystemExit, чтобы прежние писатели не изменились."""


def lock_path() -> Path:
    """Путь лока карты — из конфига, а не из аргумента вызывающего: забытый или
    чужой путь лока оставил бы запись без общего замка."""
    from config import из_файла, конфиг, путь_harness_conf
    settings = конфиг()
    subdir = (settings.get("SERVICE_LOCKS_SUBDIR") or os.environ.get("SERVICE_LOCKS_SUBDIR")
              or из_файла(путь_harness_conf()).get("SERVICE_LOCKS_SUBDIR") or "services")
    return Path(settings["LOG_DIR"]) / "locks" / subdir / "dev-map.lock"


@contextlib.contextmanager
def map_lock(path: Path, wait_sec: float | None = None):
    """Эксклюзивный flock. `wait_sec=None` — ждать без предела (как всегда);
    число — попытки LOCK_NB с шагом 0,2 с и LockBusy по истечении: лок карты
    держат демоны по полчаса, и сторожу, который ждёт молча, нельзя стоять."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        if wait_sec is None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait_sec
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockBusy(str(path))
                    time.sleep(0.2)
        yield


def task_ids(data: dict) -> set:
    return {t.get("id") for line in data.get("lines") or [] for t in line.get("tasks") or []}


def insert_position(lines: list, line_id: str) -> int:
    """Номер строки, ПЕРЕД которой встаёт новая карточка: первая задача своей
    линии. Вставка перед якорем не трогает ни одной существующей строки —
    правка структурного файла регулярками съедала соседей (09.09.2026)."""
    in_line = False
    for number, line in enumerate(lines):
        if re.match(rf"^\s*-\s+id:\s*{re.escape(line_id)}\s*$", line):
            in_line = True
            continue
        if in_line and re.match(r"^\s{6}-\s+id:\s", line):
            return number
        if in_line and re.match(r"^\s{0,4}-\s+id:\s", line):
            break          # началась следующая линия, задач в нашей нет
    # MapRefused, а не голый SystemExit: писатель тревог ловит именно его и кладёт
    # намерение в очередь; на линии без задач (sample-29, app-cms) иначе оно терялось.
    raise MapRefused(f"в карте не нашлось места для задачи линии «{line_id}»")


def insert_cards(text: str, line_id: str, cards: list) -> str:
    """Текст карты с карточками перед первой задачей линии. Сверка числом и id
    стоит здесь, одна на обоих писателей."""
    before = yaml.safe_load(text) or {}
    before_ids = task_ids(before)
    lines = text.splitlines(keepends=True)
    at = insert_position(lines, line_id)
    lines[at:at] = ["".join(c) for c in cards]
    new_text = "".join(lines)
    after_ids = task_ids(yaml.safe_load(new_text) or {})
    lost = before_ids - after_ids
    if lost:
        raise MapRefused(f"ОТКАЗ: правка потеряла задачи: {sorted(lost)}")
    if len(after_ids) != len(before_ids) + len(cards):
        raise MapRefused(f"ОТКАЗ: задач было {len(before_ids)}, стало "
                         f"{len(after_ids)}, добавляли {len(cards)}")
    return new_text


def write_cards_locked(map_path, line_id, cards, *, recheck=None, lock_wait_sec=None,
                       lock_file=None, committed=None) -> bool:
    """Пишет карточки в карту под общим локом. False — писать уже нечего.

    `recheck(текст карты)` зовётся ПОД локом: между проверкой снаружи и записью
    карточку мог завести другой зов. `cards` — список или функция от текста
    карты (id нельзя занять заранее, пока лок не взят). `committed()` зовётся
    под тем же локом после записи: журнал исхода ложится до того, как лок
    возьмёт соседний зов. `lock_file` нужен только пробам, у боевых зовов путь
    берётся из конфига."""
    map_path = Path(map_path)
    with map_lock(Path(lock_file) if lock_file else lock_path(), lock_wait_sec):
        text = map_path.read_text(encoding="utf-8")
        if recheck is not None and not recheck(text):
            return False
        chosen = cards(text) if callable(cards) else cards
        map_path.write_text(insert_cards(text, line_id, chosen), encoding="utf-8")
        if committed is not None:
            committed()
        return True


def open_card_by_source(map_text: str, source: str) -> dict | None:
    """Открытая (не done/принято) карточка с этим источником либо None. Статус
    беклог — тоже открытая: решать за владельца, что с ней делать, нельзя."""
    data = yaml.safe_load(map_text) or {}
    for line in data.get("lines") or []:
        for task in line.get("tasks") or []:
            if task.get("источник") == source and task.get("status") not in CLOSED_STATUSES:
                return dict(task, _line=line.get("id"))
    return None


def parse_journal(lines) -> list:
    """Строки журнала тревог как словари; битая строка пропускается."""
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def fold_journal(rows: list) -> dict:
    """{ключ: состояние} за ОДИН проход журнала.

    Состояние: judged — последнее судимое событие, has_card — рождалась ли
    карточка, birth — последнее рождение, repeats — повторов после него.
    Разбор журнала на каждый ключ отдельно растёт как ключи × строки, а осмотр
    очереди зовёт это на каждом вызове."""
    folded: dict = {}
    for row in rows:
        key = row.get("ключ")
        if not key:
            continue
        item = folded.setdefault(key, {"judged": None, "has_card": False,
                                       "birth": None, "repeats": 0})
        event = row.get("событие")
        if event in OPEN_EVENTS or event == GREEN_EVENT:
            item["judged"] = event
        if event in BIRTH_EVENTS and row.get("id"):
            item["has_card"] = True
            item["birth"] = row
            item["repeats"] = 0
        elif event == "повтор":
            item["repeats"] += int(row.get("число") or 1)
    return folded


def state_of(item: dict | None) -> str:
    if not item or item["judged"] is None:
        return "нет"
    if item["judged"] == GREEN_EVENT:
        return "зелёное"
    return "открыта" if item["has_card"] else "нет"


def key_state(lines, key: str) -> str:
    """«открыта» | «зелёное» | «нет» — ОДИН судья для писателя, веса и приёмки."""
    rows = [r for r in parse_journal(lines) if r.get("ключ") == key]
    return state_of(fold_journal(rows).get(key))


COMPACT_AGE_SEC = 86400


def compact_rows(rows: list, now: float) -> list:
    """Подряд идущие повторы одного ключа старше суток сливаются в одну строку
    с полем «число». Порядок событий ключа и его состояние не меняются: сливается
    только серия повторов без рождений и зелёных между ними. Свежие повторы
    остаются дословно — «200 зовов = 200 строк» верно за последние сутки."""
    out: list = []
    last_of: dict = {}
    for row in rows:
        key = row.get("ключ")
        old = epoch(row.get("ts")) < now - COMPACT_AGE_SEC
        prev = last_of.get(key)
        if (key and row.get("событие") == "повтор" and old and prev is not None
                and prev.get("событие") == "повтор" and prev.get("_old")):
            prev["число"] = int(prev.get("число") or 1) + int(row.get("число") or 1)
            prev["ts"] = row.get("ts")
            continue
        entry = dict(row, _old=old) if key else dict(row)
        out.append(entry)
        if key:
            last_of[key] = entry
    return [{k: v for k, v in r.items() if k != "_old"} for r in out]


def epoch(stamp) -> float:
    try:
        return datetime.datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError:
        return 0.0


def alert_weights(journal_path: str, data: dict) -> dict:
    """{id: ключ} для ОДНОЙ карточки — старшей открытой тревоги.

    Признак срочности ставит демон (журнал), а не смена (карта): поле в карте
    пишет тот, кому оно выгодно. Журнал и карта сверяются в обе стороны."""
    try:
        lines = Path(journal_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    folded = fold_journal(parse_journal(lines))
    tasks = {t.get("id"): t for line in data.get("lines") or [] for t in line.get("tasks") or []}
    found = []
    for key, item in folded.items():
        if state_of(item) != "открыта":
            continue
        birth = item["birth"]
        task = tasks.get(birth["id"])
        if (task and task.get("источник") == ALERT_SOURCE_PREFIX + key
                and task.get("status") not in NOT_WEIGHTED_STATUSES):
            found.append((epoch(birth.get("ts")), birth["id"], key))
    if not found:
        return {}
    _, card_id, key = min(found)
    return {card_id: key}


# ── перенесено из map-from-message.py ──────────────────────────────────────

from config import конфиг  # noqa: E402
from neighbor import модуль  # noqa: E402


def транслит(слово: str) -> str:
    """Запасной перевод имени — ОБЩИМ прибором: вторая таблица дала бы два
    разных имени для одного слова ([[the-install-does-not-reproduce]])."""
    return модуль("transliterate").перевести(слово)


def идентификатор(приставка: str, заголовок: str, занятые: set,
                  имя_англ: str = "") -> str:
    """Ключ карточки. Имя от модели — английское; без неё транслит.

    Имена в карте всегда латиницей (слово владельца 11.09.2026), а транслит
    английским не считается — поэтому английское имя спрашивается у модели в
    том же вызове, что и задачи. Запасной путь нужен: модель молчит или
    уперлась в потолок, а исход обязан быть всегда, иначе гейт красен. Замер
    13.10.2026: 29 ключей в карте были кириллицей.
    """
    чистое = "-".join(re.findall(r"[a-z0-9]+", (имя_англ or "").lower()))[:60]
    if чистое:
        основа = чистое
    else:
        основа = "-".join(транслит(с) for с in
                          re.findall(r"[а-яёa-z0-9]+", заголовок.lower())[:5]) or "task"
    ид = f"{приставка}.{основа}"
    номер = 2
    while ид in занятые:
        ид = f"{приставка}.{основа}-{номер}"
        номер += 1
    return ид


# Значения, которые нельзя копировать в карту НИКОГДА: наши живые ключи.
#
# Владелец присылает их прямо в канал (29.09.2026 — ключ учёта), а карточка
# заводится дословной цитатой его сообщения. Карта едет в гит и в контекст
# каждой сессии, поэтому ключ вырезается ЗДЕСЬ, у источника, а не только
# гейтом секретов на коммите: между записью карты и коммитом памяти нет
# человека, который бы это заметил.
#
# Короткие значения не берём: имя бота и номер чата дали бы совпадение на
# ровном месте. Каталог ключей — из паспорта установки, как и у гейта.
МЕТКА_КЛЮЧА = "<ключ вырезан: лежит в каталоге ключей>"


def без_ключей(текст: str, каталог_ключей: str | None = None) -> str:
    """Вырезать из текста значения наших ключей. Не находим каталог — не
    молчим: пустой ответ и сообщение о причине сделали бы утечку тихой."""
    try:
        каталог = Path(каталог_ключей or конфиг().get("SECRETS_DIR") or "")
    except Exception:
        return текст
    if not каталог.is_dir():
        return текст
    for файл in sorted(каталог.iterdir()):
        if not файл.is_file():
            continue
        try:
            содержимое = файл.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for строка in содержимое.splitlines():
            значение = строка.strip()
            if len(значение) >= 16 and значение in текст:
                текст = текст.replace(значение, МЕТКА_КЛЮЧА)
    return текст


def глубина_строкой(текст: str, сегодня: str) -> str:
    """Заготовка глубины для новой карточки — одной строкой YAML.

    Владелец 13.09.2026: «это должно быть не просто памятью, должен работать
    чёткий механизм». Правило «пересмотр сразу ставит задаче статус и глубину»
    держалось вниманием смены: замер того дня — 15 карточек из 21, заведённых
    отсюда, поля «глубина» не имели вовсе, и 12 из них уже дошли до статуса
    test. Поле дописывалось руками, когда краснел гейт.

    Судья один — pipeline.py: своя копия правил здесь стала бы четвёртой и
    разошлась бы с остальными молча.
    """
    import importlib.util
    спец = importlib.util.spec_from_file_location(
        "пайплайн", Path(__file__).resolve().parent.parent / "pipeline.py")
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    поля = модуль.глубина_для_карточки(текст, сегодня)
    внутри = ", ".join(f"{к}: {в}" for к, в in поля.items())
    return "{ " + внутри + " }"


def приставка_линии(линия: dict) -> str:
    """Приставка id берётся у соседей той же линии: в «hr.*» новая задача не
    имеет права называться «pr.*»."""
    for з in линия.get("tasks") or []:
        ид = str(з.get("id") or "")
        if "." in ид:
            return ид.split(".", 1)[0]
    return str(линия.get("id") or "task")[:2]


место_вставки = insert_position


def _selftest() -> int:
    from selftest_cases import Cases
    случаи = Cases()
    проба = случаи.check

    карта = (
        "tasks:\n"
        "  - id: первая\n"
        "    status: wip\n"
        "    summary: \"раз\"\n"
        "\n"                      # ПУСТАЯ СТРОКА — она и ломала прежний разбор
        "  - id: вторая\n"
        "    status: test\n"
        "lines:\n"
        "  - имя: линия\n")
    все = блоки(карта)
    проба("найдены обе карточки", ["первая", "вторая"], [и for и, _, _, _ in все])
    # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: перед карточкой пустая строка, и блок соседа
    # утаскивал её вместе с собой.
    проба("первая карточка не глотает вторую", False, "вторая" in все[0][1])
    проба("последняя карточка не утаскивает соседнюю секцию", False,
          "lines:" in все[1][1])
    проба("блок по id", "вторая", блок(карта, "вторая")[0].split("\n")[0].split(": ")[1])
    проба("нет такой карточки — None", None, блок(карта, "нет-такой"))
    проба("поле читается без кавычек", "раз", поле(все[0][1], "summary"))
    проба("нет поля — None", None, поле(все[0][1], "провал_проверки"))
    проба("статус читается", "test", поле(все[1][1], "status"))

    # Инлайн-форма карточки (вся в одной строке) картой тоже используется:
    # её блок — ровно эта строка, и соседей она не трогает.
    плоская = ("tasks:\n"
               "  - { id: плоская, status: wip }\n"
               "  - id: следом\n"
               "    status: plan\n")
    проба("инлайн-карточка не мешает соседней", ["следом"],
          [и for и, _, _, _ in блоки(плоская)])

    print(f"SELFTEST: {'зелёный' if случаи.ok else 'КРАСНЫЙ'} ({случаи.paths} путей; "
          "больной первый — пустая строка перед карточкой)")
    return 0 if случаи.ok and _пробы_писателя() == 0 else 1


def _пробы_писателя() -> int:
    """Пробы общего писателя карты живут в test_cards_lib.py рядом со скриптами;
    самотест библиотеки обязан их исполнять, иначе они зелёные в никуда."""
    import subprocess
    проба = Path(__file__).resolve().parent.parent / "test_cards_lib.py"
    if not проба.is_file():
        print(f"ПРОБЫ ПИСАТЕЛЯ: нет {проба.name}")
        return 1
    # Проба сама зовёт этот самотест: метка гасит круг, где самотест снова
    # запустил бы пробу, а та самотест.
    окружение = dict(os.environ, CARDS_LIB_PROBE_NESTED="1")
    сделано = subprocess.run([sys.executable, str(проба)], capture_output=True,
                             text=True, env=окружение, timeout=600)
    print(сделано.stdout.rstrip().splitlines()[-1] if сделано.stdout.strip()
          else "ПРОБЫ ПИСАТЕЛЯ: пустой вывод")
    return сделано.returncode


def главная(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    if "--границы" in argv:
        # Для писателей на оболочке: печатает номер первой и последней строки
        # блока (1-based, конец включительно) — им нужна построчная хирургия.
        ид, путь = argv[argv.index("--границы") + 1:argv.index("--границы") + 3]
        текст = open(путь, encoding="utf-8").read()
        найдено = блок(текст, ид)
        if not найдено:
            print(f"карточки «{ид}» в {путь} нет", file=sys.stderr)
            return 1
        тело, начало, конец = найдено
        первая = текст[:начало].count("\n") + 1
        последняя = первая + тело.rstrip("\n").count("\n")
        print(f"{первая} {последняя}")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(главная(sys.argv[1:]))
