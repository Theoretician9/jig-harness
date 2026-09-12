#!/usr/bin/env python3
"""Гигиена того, что едет в контекст каждой сессии.

Откуда взят: UNIFIED/templates/check-context-hygiene.py (живой гейт боевого
сервера). Что изменено: слаг проекта и место памяти читаются из
/etc/harness/install.conf и /etc/harness/harness.conf (мини-парсер ниже,
конфиги не исполняются; env HARNESS_INSTALL_CONF / HARNESS_CONF — для тестов);
память нового харнеса живёт в $PROJECT_DIR/<MEMORY_DIR_NAME>, а не в
~/.claude/projects/ — путь берётся из harness.conf, данные, не код.

Улика шаблона: за один день всплыли три вещи одного рода — записка для новой
сессии разрослась до 172 тысяч символов, рядом обнаружилась вторая,
осиротевшая память, и обе выросли из одного: правило существовало, но
держалось вниманием. Разбирать это руками раз в два месяца означает
согласиться разбирать вечно, поэтому проверка стоит в гейте.

Проверяется ровно то, что уже ломалось. Защиты от воображаемых бед здесь нет.

Запуск: python3 scripts/check-context-hygiene.py [--repo <путь>]
Код возврата 1 — есть нарушение, блокирующее коммит.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

# ОТКУДА ЧИСЛА. Вопрос владельца 10.09.2026 (П-4): «потолок держит какой
# инвариант?» — иначе гейт краснеет по произвольному числу и его хочется
# заглушить автоархивацией, то есть выключить датчик, не поняв сигнала.
#
# Инвариант один: СТАРТ СЕССИИ ДЁШЕВ. В каждую сессию грузятся четыре файла —
# CLAUDE.md, STATE.md, УКАЗАНИЯ.md и индекс памяти. Их сумма на 10.09.2026 —
# 48 024 знака ≈ 16 000 токенов, и это плата за КАЖДУЮ сессию (5,4 ротации в
# сутки). Потолки — доли этой суммы: состояние 10k, указания 8k, индекс 25k.
# Растёт файл — растёт цена каждой смены, поэтому сигнал остаётся кричащим:
# краснеет он не «слишком часто», а ровно тогда, когда цена выросла.
#
# У dev-map.yaml инвариант ДРУГОЙ: карта в контекст не грузится (@-include её
# не берёт), но агент читает её целиком, когда правит. Потолок карты считает
# код от числа задач: чтение карты — цена одного шага, а не постоянная плата,
# поэтому запас на задачу щедрый (2 500 знаков ≈ 800 токенов).
STATE_MAX = 10_000  # символов; всё, что не «сейчас», уезжает в docs/handover/
# Указания владельца грузятся в каждую сессию целиком, поэтому потолок жёстче
# STATE.md: файл обязан оставаться списком действующих правил, а не архивом
# переписки. Снятое указание убирает владелец, разобранное без правила —
# реестр в хвосте файла (scripts/ukazaniya.py).
ORDERS_MAX = 8_000
INDEX_MAX = 25_000  # индекс памяти грузится каждую сессию — та же болезнь, мягкий порог


def read_conf(path: str, conf: dict) -> None:
    """Мини-парсер KEY="value". Не source: гейту нельзя исполнять чужой код."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"""\s*([A-Z][A-Z0-9_]*)=("[^"]*"|'[^']*'|[^#\s]*)""", line)
                if m:
                    raw = m.group(2)
                    val = raw[1:-1] if raw[:1] in "\"'" else raw
                    for name, seen in conf.items():
                        val = val.replace("${%s}" % name, seen).replace("$" + name, seen)
                    conf[m.group(1)] = val
    except OSError:
        pass


CONF: dict = {}
read_conf(os.environ.get("HARNESS_INSTALL_CONF", "/etc/harness/install.conf"), CONF)
read_conf(os.environ.get("HARNESS_CONF", "/etc/harness/harness.conf"), CONF)


class Report:
    def __init__(self):
        self.blocking: list[str] = []
        self.warnings: list[str] = []

    def block(self, msg: str) -> None:
        self.blocking.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def chars(path: Path) -> int:
    return len(path.read_text(encoding="utf-8"))


def memory_home(repo: Path) -> Path:
    return repo / (CONF.get("MEMORY_DIR_NAME") or "память")


def check_state_size(repo: Path, r: Report) -> None:
    state = repo / "STATE.md"
    if not state.exists():
        r.block("STATE.md не найден — на него смотрит @-include в CLAUDE.md")
        return
    size = chars(state)
    if size > STATE_MAX:
        r.block(
            f"STATE.md разросся: {size} символов при потолке {STATE_MAX}.\n"
            f"    Вытесни завершённое в docs/handover/SESSION-HANDOFF-<дата>.md — STATE.md держит только «что верно сейчас»."
        )


def check_orders_size(repo: Path, r: Report) -> None:
    """Указания владельца — файл контекста, а не архив переписки."""
    orders = memory_home(repo) / "УКАЗАНИЯ.md"
    if not orders.exists():
        r.block("память/УКАЗАНИЯ.md не найден — на него смотрит @-include в CLAUDE.md")
        return
    size = chars(orders)
    if size > ORDERS_MAX:
        r.block(
            f"память/УКАЗАНИЯ.md разросся: {size} символов при потолке {ORDERS_MAX}.\n"
            f"    Держит только ДЕЙСТВУЮЩИЕ указания; снятое убирает владелец, "
            f"подробности уезжают в запись памяти."
        )


def check_single_memory(repo: Path, r: Report) -> None:
    """Память проекта должна быть в одном месте.

    Улика шаблона: осиротевший .claude/memory/ прожил в репозитории 2,5 месяца
    после того, как Claude Code сменил место хранения. Его инварианты разошлись
    с реальностью: правило «никакого учебного режима» осталось записанным,
    когда тренировочный тур уже работал.
    """
    home = memory_home(repo)
    stray = repo / ".claude/memory"
    if stray.exists() and stray.resolve() != home.resolve():
        r.block(
            f"В репозитории вторая память: {stray}.\n"
            f"    Память живёт только в {home}. Перенеси недостающее и удали каталог."
        )


def check_memory_index(repo: Path, r: Report) -> None:
    # MEMORY_INDEX_NAME в harness.conf задан путём от корня проекта
    # («память/MEMORY.md»); голое имя без слэша понимается как файл внутри
    # каталога памяти — обе формы конфига законны.
    name = CONF.get("MEMORY_INDEX_NAME") or "MEMORY.md"
    index = repo / name if "/" in name else memory_home(repo) / name
    if not index.exists():
        r.warn(f"Индекс памяти не найден: {index}")
        return
    size = chars(index)
    if size > INDEX_MAX:
        r.warn(
            f"Индекс памяти {size} символов при ориентире {INDEX_MAX} — он едет в контекст каждую сессию.\n"
            f"    Пора прополоть: удалить устаревшие факты, слить дубли."
        )


def check_zapisi_ne_osiroteli(repo: Path, r: "Report") -> None:
    """Каждая запись памяти названа хотя бы в одном указателе.

    Указателей стало два (общий и продуктовый, 11.09.2026): разделение сняло
    потолок, но завело новую беду — запись, выпавшая из ОБОИХ, не находится
    вовсе и не срабатывает никогда. Это потеря знания, а не лишний файл.
    """
    дом = memory_home(repo)
    if not дом.is_dir():
        return
    указатели = [п for п in дом.glob("*.md") if это_указатель(п.name)]
    текст = "\n".join(п.read_text(encoding="utf-8", errors="replace")
                      for п in указатели)
    сироты = sorted(п.name for п in дом.glob("*.md")
                    if not это_указатель(п.name) and п.stem not in текст)
    if сироты:
        r.block("записи памяти нет ни в одном указателе (найти их будет нечем): "
                + ", ".join(сироты[:8])
                + ("…" if len(сироты) > 8 else "")
                + "\n    Внести строку в один из указателей ("
                + ", ".join(sorted(п.name for п in указатели)) + ") либо удалить запись.")


# Указатель памяти узнаётся по ФОРМЕ ИМЕНИ, а не по списку имён: общий
# «MEMORY.md»/«УКАЗАНИЯ.md» и продуктовые «<ИМЯ>-ПАМЯТЬ.md»/«<ИМЯ>-УКАЗАНИЯ.md».
#
# Владелец 12.09.2026 велел проверить, что харнес очищен от имени продукта.
# Прежде здесь стоял список имён вместе с именем продукта — имя чужого
# проекта в общем инструменте. Выводить его из PRODUCT_DIR нельзя: каталог
# зовётся латиницей, а файлы памяти — кириллицей, и приведение регистра
# дало бы промах и молчание. Форма имени решает это без единой настройки.
УКАЗАТЕЛЬ_ПАМЯТИ = re.compile(r"^(MEMORY|УКАЗАНИЯ)\.md$|-(ПАМЯТЬ|УКАЗАНИЯ)\.md$")


def это_указатель(имя: str) -> bool:
    """Файл памяти — указатель (его не надо самому упоминать в указателе)."""
    return bool(УКАЗАТЕЛЬ_ПАМЯТИ.search(имя))


def задач_в_карте(devmap: Path) -> int:
    """Сколько задач в карте. Битый YAML — считаем по строкам «- id:»."""
    текст = devmap.read_text(encoding="utf-8", errors="replace")
    return текст.count("\n      - id: ") or текст.count("- id: ")


def потолок_карты(devmap: Path) -> int:
    """Потолок считается ОТ ЧИСЛА ЗАДАЧ, а не константой.

    Владелец 11.09.2026: «Зачем это мне решать? Это должно работать
    автоматически <…> и тоже определяться кодом». Константу приходилось
    двигать руками при каждом росте реестра (30 000 → 240 000 за месяц), и
    каждый такой сдвиг — подгонка датчика под факт. Болезнь, ради которой
    гейт стоит, — не число задач (их столько, сколько работы), а РАЗДУТАЯ
    запись: описание на три экрана вместо сути. Поэтому запас даётся на
    задачу, а сверх него проверяется самая длинная запись.
    """
    база = int(CONF.get("DEVMAP_BASE_CHARS") or 20_000)
    на_задачу = int(CONF.get("DEVMAP_CHARS_PER_TASK") or 2_500)
    return база + задач_в_карте(devmap) * на_задачу


def check_devmap_size(repo: Path, r: Report) -> None:
    """dev-map.yaml — реестр живых задач, не архив: он едет в контекст сессии
    и дашборда, разросшийся глушит обоих той же болезнью, что и STATE.md.

    Файла нет — не ошибка: продукт мог быть ещё не начат.
    Потолок считает `потолок_карты` от числа задач; запас на задачу —
    DEVMAP_CHARS_PER_TASK из harness.conf (данные, не код).
    """
    devmap = repo / "dev-map.yaml"
    if not devmap.exists():
        return
    limit = потолок_карты(devmap)
    size = chars(devmap)
    if size > limit:
        r.block(
            f"dev-map.yaml разросся: {size} символов при потолке {limit} "
            f"({задач_в_карте(devmap)} задач × {CONF.get('DEVMAP_CHARS_PER_TASK') or 2500} "
            f"+ база). Средняя запись длиннее запаса — сократи описания или "
            f"заархивируй закрытые направления в docs/dev-map-archive.yaml."
        )


def check_includes_alive(repo: Path, r: Report) -> None:
    """@-include на несуществующий файл — тихая потеря контекста, а не ошибка старта.

    Второй способ потерять контекст молча — путь, который оболочка не считает
    импортом. Улика 20.08.2026: `@память/УКАЗАНИЯ.md` и `@память/MEMORY.md`
    стояли в CLAUDE.md, файлы существовали, гейт был зелёным — а в контекст
    сессии не приходили. Замер тремя прогонами `claude -p` на фикстурах:
    `@ascii.md` доезжает, `@dir-ascii/УКАЗАНИЯ.md` доезжает (кириллица в имени
    файла законна), `@память/rules.md` — нет, `@./память/УКАЗАНИЯ.md` — да.
    Разбор пути обрывается на не-ASCII В НАЧАЛЕ: лечится префиксом `./`.
    Цена промаха — ровно та задача владельца, ради которой указания заводили:
    правило пережило ротацию на диске и не пережило её в контексте.
    """
    claude_md = repo / "CLAUDE.md"
    if not claude_md.exists():
        r.block("CLAUDE.md не найден")
        return
    for n, line in enumerate(claude_md.read_text(encoding="utf-8").split("\n"), 1):
        if not line.startswith("@"):
            continue
        path = line[1:].strip()
        if path and not path[0].isascii():
            r.block(
                f"CLAUDE.md:{n} импорт не сработает — путь начинается с не-ASCII: {line.strip()}\n"
                f"    Оболочка молча пропустит его, файл в контекст не поедет. Напиши @./{path}"
            )
            continue
        if not (repo / path).exists():
            r.block(f"CLAUDE.md:{n} подключает несуществующий файл: {line.strip()}")


def заполнение(repo: Path) -> dict:
    """Файл → сколько знаков, потолок и доля. ОДИН счёт на всех читателей.

    Нужно не только гейту: ревизия памяти запускается по заполнению, а не
    только по календарю (потолок достигается за две недели, ревизия ходит раз
    в месяц). Второй счёт в другом файле разошёлся бы с этим молча.
    """
    карта = repo / "dev-map.yaml"
    limit = потолок_карты(карта) if карта.exists() else 30_000
    готово = {}
    for файл, потолок in ((repo / "STATE.md", STATE_MAX),
                          (memory_home(repo) / "УКАЗАНИЯ.md", ORDERS_MAX),
                          (memory_home(repo) / "MEMORY.md", INDEX_MAX),
                          (repo / "dev-map.yaml", limit)):
        if файл.exists():
            готово[файл.name] = {"знаков": chars(файл), "потолок": потолок,
                                 "доля": round(chars(файл) / потолок, 4)}
    return готово


def переполнены(repo: Path, порог: float) -> list:
    """Готовые строки предложений по файлам, упёршимся в потолок.

    Строку собирает тот, кто считает заполнение: ревизия памяти звала бы это
    вложенным питоном внутри bash, и первая же кавычка ломала бы демона.
    """
    строки = []
    for имя, з in sorted(заполнение(repo).items()):
        if з["доля"] >= порог:
            строки.append(
                f"{имя}: {з['знаков']} из {з['потолок']} знаков "
                f"({з['доля'] * 100:.0f} %) → ПРЕДЛОЖЕНИЕ: прополоть до 80 % — "
                f"перенести отработавшее в архив или удалить, иначе ближайший "
                f"коммит упрётся в гейт гигиены")
    return строки


def близко_к_потолку(repo: Path, r: "Report") -> None:
    """Предупредить ДО того, как гейт покраснеет в момент коммита.

    68 покраснений гигиены за месяц — не «слишком строгий порог», а поздний
    сигнал: файл упирается в потолок ровно тогда, когда работа уже сделана и
    коммит готов. Владелец (П-4, 10.09.2026) справедливо возразил против
    автоархивации: она делает гейт никогда-не-краснеющим, то есть выключает
    датчик. Ранний сигнал решает ту же боль, ничего не выключая: с 90 %
    заполнения файл называется по имени, но коммит идёт.
    """
    for имя, з in заполнение(repo).items():
        if 0.9 <= з["доля"] <= 1.0:
            r.warn(f"{имя}: {з['знаков']} из {з['потолок']} знаков — "
                   f"{з['доля'] * 100:.0f} % потолка, пора прополоть до того, "
                   f"как гейт покраснеет")


def стенд(tmp: str) -> Path:
    """Здоровый минимум репозитория: всё, чего гейт вправе требовать."""
    repo = Path(tmp) / "repo"
    (repo / "docs" / "handover").mkdir(parents=True)
    memory_home(repo).mkdir(parents=True)
    (repo / "CLAUDE.md").write_text(
        "# правила\n@STATE.md\n@./память/УКАЗАНИЯ.md\n@./память/MEMORY.md\n",
        encoding="utf-8")
    (repo / "STATE.md").write_text("состояние\n", encoding="utf-8")
    (repo / "dev-map.yaml").write_text("epics: []\n", encoding="utf-8")
    (memory_home(repo) / "УКАЗАНИЯ.md").write_text("указания\n", encoding="utf-8")
    (memory_home(repo) / "MEMORY.md").write_text("указатель\n", encoding="utf-8")
    return repo


def оценить(repo: Path) -> "Report":
    r = Report()
    близко_к_потолку(repo, r)
    check_zapisi_ne_osiroteli(repo, r)
    check_state_size(repo, r)
    check_orders_size(repo, r)
    check_single_memory(repo, r)
    check_memory_index(repo, r)
    check_devmap_size(repo, r)
    check_includes_alive(repo, r)
    return r


def самотест() -> int:
    """Больные случаи на подставном репозитории.

    До 10.09.2026 у гейта не было ни одного: «доказательством» служил прогон на
    живом дереве, где он зелёный всегда, и обезвредить его можно было молча
    (нашла мутационная проба).
    """
    import tempfile
    ok = True

    def проба(имя: str, ждём: str, r: "Report"):
        nonlocal ok
        было = "блок" if r.blocking else ("предупреждение" if r.warnings else "чисто")
        if было == ждём:
            print(f"  ок    {имя}")
        else:
            print(f"  ПЛОХО {имя}: ждали «{ждём}», получили «{было}»"
                  f" {r.blocking or r.warnings}")
            ok = False

    with tempfile.TemporaryDirectory() as tmp:
        проба("здоровое дерево — чисто", "чисто", оценить(стенд(tmp)))
    # БОЛЬНОЙ СЛУЧАЙ: STATE.md растёт «ещё на абзац» и вытесняет собой контекст.
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (repo / "STATE.md").write_text("я" * (STATE_MAX + 1), encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: STATE.md перерос потолок — отказ", "блок", оценить(repo))
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (repo / "STATE.md").write_text("я" * int(STATE_MAX * 0.95), encoding="utf-8")
        проба("95 % потолка — ранний сигнал, но не отказ", "предупреждение", оценить(repo))
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (repo / "STATE.md").unlink()
        проба("STATE.md пропал — на него смотрит @-include", "блок", оценить(repo))
    # БОЛЬНОЙ СЛУЧАЙ 20.08.2026: путь с кириллицы в начале в контекст не едет.
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (repo / "CLAUDE.md").write_text("@память/MEMORY.md\n", encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: @-путь без «./» перед кириллицей — отказ",
              "блок", оценить(repo))
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (repo / ".claude" / "memory").mkdir(parents=True)
        проба("вторая память в .claude/memory — отказ", "блок", оценить(repo))
    # Указателей памяти стало ДВА (общий и продуктовый, 11.09.2026), и запись,
    # выпавшая из обоих, не находится вовсе: ссылки на неё нет, значит она не
    # сработает никогда. Это потеря знания, а не мусор.
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (memory_home(repo) / "сирота.md").write_text("факт\n", encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: запись без ссылки ни в одном указателе — отказ",
              "блок", оценить(repo))
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (memory_home(repo) / "живая.md").write_text("факт\n", encoding="utf-8")
        (memory_home(repo) / "MEMORY.md").write_text(
            "указатель [[живая]]\n", encoding="utf-8")
        проба("запись, на которую ссылается указатель, — чисто", "чисто", оценить(repo))
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (memory_home(repo) / "своя.md").write_text("факт\n", encoding="utf-8")
        (memory_home(repo) / "ПРОДУКТ-ПАМЯТЬ.md").write_text(
            "продуктовый указатель [[своя]]\n", encoding="utf-8")
        проба("ссылка из ВТОРОГО указателя тоже считается", "чисто", оценить(repo))
    with tempfile.TemporaryDirectory() as tmp:
        # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: указатель узнаётся по ФОРМЕ имени. Прежде
        # список имён был вшит вместе с именем продукта, и у другого проекта
        # его указатель считался бы сиротой — то есть гейт краснел бы на
        # здоровом файле, а такой гейт отключают.
        repo = стенд(tmp)
        (memory_home(repo) / "чужая.md").write_text("факт\n", encoding="utf-8")
        (memory_home(repo) / "МОЙПРОЕКТ-УКАЗАНИЯ.md").write_text(
            "указатель другого проекта [[чужая]]\n", encoding="utf-8")
        проба("указатель незнакомого проекта узнан по форме имени", "чисто", оценить(repo))
    # БОЛЬНОЙ СЛУЧАЙ 11.09.2026: карта растёт числом задач — это здоровье, а
    # раздутой записью — болезнь. Константный потолок их не различал, и его
    # двигали руками (30 000 → 240 000 за месяц).
    def карта(задач: int, знаков_на_задачу: int) -> str:
        тело = "epics:\n  - id: e1\n    tasks:\n"
        for i in range(задач):
            тело += f"      - id: t{i}\n        status: plan\n"
            тело += f"        summary: \"{'я' * знаков_на_задачу}\"\n"
        return тело

    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (repo / "dev-map.yaml").write_text(карта(200, 1000), encoding="utf-8")
        проба("200 задач по сути — рост реестра не болезнь", "чисто", оценить(repo))
    with tempfile.TemporaryDirectory() as tmp:
        repo = стенд(tmp)
        (repo / "dev-map.yaml").write_text(карта(20, 9000), encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: 20 задач с описанием на три экрана — отказ",
              "блок", оценить(repo))

    print("SELFTEST: зелёный (11 путей, среди них больные: потолок STATE, @-путь, раздутая запись карты, запись памяти без указателя)"
          if ok else "SELFTEST: КРАСНЫЙ")
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return самотест()
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=CONF.get("PROJECT_DIR") or str(Path.cwd()))
    ap.add_argument("--доли", action="store_true",
                    help="заполнение постоянных файлов машинно (JSON)")
    ap.add_argument("--переполнены", type=float, metavar="ПОРОГ",
                    help="строки предложений по файлам с долей не ниже порога")
    доводы = ap.parse_args()
    repo = Path(доводы.repo)
    if доводы.доли:
        print(json.dumps(заполнение(repo), ensure_ascii=False))
        return 0
    if доводы.переполнены is not None:
        for строка in переполнены(repo, доводы.переполнены):
            print(строка)
        return 0

    r = Report()
    близко_к_потолку(repo, r)
    check_zapisi_ne_osiroteli(repo, r)
    check_state_size(repo, r)
    check_orders_size(repo, r)
    check_single_memory(repo, r)
    check_memory_index(repo, r)
    check_devmap_size(repo, r)
    check_includes_alive(repo, r)

    for w in r.warnings:
        print(f"[гигиена] ⚠ {w}")
    for b in r.blocking:
        print(f"[гигиена] ✗ {b}", file=sys.stderr)

    if r.blocking:
        return 1
    print("[гигиена] контекст в порядке" + (f" ({len(r.warnings)} предупр.)" if r.warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
