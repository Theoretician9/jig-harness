#!/usr/bin/env python3
"""Приёмка задач: что доказано механизмом, а что остаётся владельцу.

Владелец 11.09.2026: «Нужен механизм фильтрации, чтобы ты мне давал из этого
списка именно то, что надо проверить, а там где большая уверенность, проверено
тобой конкретно что точно работает, то там сам закрывай проверку».

Замер, который задаёт задачу: в карте 105 задач, 89 в статусе `test`, а
проверена владельцем ОДНА. Список «проверь меня» перестал быть списком.

## Чем доказывается

Способ проверки — ДАННЫЕ в карточке задачи, поле `проверка:`:

    проверка:
      команда: "python3 scripts/tasks-from-channel.py --selftest"
      ждём: "САМОТЕСТ ПРОЙДЕН"

Почему данными, а не догадкой по тексту: замер 11.09.2026 показал, что признак
способа находится в прозе у 84 задач из 89 — то есть не находится ни у одной.
Слово «ворота» в описании не делает задачу проверяемой воротами.

## Что механизм НЕ делает

Не чинит, не пишет код, не трогает ничего, кроме статуса и поля с исходом
проверки (слова владельца: «Ничего не чинит, ничего не создает не пишет код, а
именно проверяет»).

Исходы (каждый говорит, ЧЕЙ это долг):
  • `доказано`       — команда отработала и вывод содержит всё ожидаемое;
  • `провал`         — команда вернула не ноль: сломан механизм, долг
                       разработчика, текст идёт ему;
  • `проверка-врёт`  — команда вернула НОЛЬ, а обещанного в выводе нет: неверно
                       обещание, долг строителя проверок (27.09.2026 так
                       провалились восемь задач на здоровых механизмах);
  • `доказать-нечем` — проверки нет или её форма негодна: долг строителя;
  • `ждёт-владельца` — доказать может только человек: это его очередь, и у неё
                       есть потолок OWNER_QUEUE_MAX.
Доказанная задача уходит в статус `принято` — «проверено машиной, вот команда».
Владельцу остаётся то, чего машина не умеет: как это выглядит на экране, удобно
ли, то ли это, что он просил. Его `done` остаётся его: `принято` — не подмена
подтверждения, а честное имя машинному доказательству.

Запуск:
    python3 scripts/acceptance.py              # сухой прогон: только отчёт
    python3 scripts/acceptance.py --применить  # + записать исходы в карту
    python3 scripts/acceptance.py --задача <id>[,<id>] [--применить]  # приёмка названных карточек под локом карты
    python3 scripts/acceptance.py --selftest
    python3 scripts/acceptance.py --мутации-задачи <id>  # шаг «мутации»: бумага .tasks/<id>/мутации.log
    python3 scripts/acceptance.py --мутации-задачи <id1>,<id2>  # пачка: один заход в срез
    python3 scripts/acceptance.py --история <id1>,<id2> --в <файл>  # чтения истории для захода в срезе (зовёт очередь)
    python3 scripts/acceptance.py --кандидаты  # «сделано, но не заявлено»: только список
Код возврата: 0 — прогон состоялся, 1 — есть провалы.
"""
from __future__ import annotations

import os
import re
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import config as конф                                    # noqa: E402
import events                                           # noqa: E402
import cards as разбор_карточки                     # noqa: E402
import patterns as формы                            # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
КАРТА = КОРЕНЬ / "dev-map.yaml"
СПИСОК_ВЛАДЕЛЬЦУ = КОРЕНЬ / "docs" / "приёмка" / "очередь-владельца.md"
ПОТОЛОК_СЕК = 120


def активное_направление() -> str:
    """Над чем работаем сейчас — спрашиваем общий загрузчик конфига.

    ДАННЫЕ, а не код: завтра активным станет продукт (владелец 12.09.2026 —
    «сейчас работать только над харнесом, чтобы продукт не мешал»). Своя
    регулярка тут была третьей копией одной и той же (ревизия лаконичности).
    """
    return конф.активная_линия()


def карточки(узел, итог=None, направление=None) -> list:
    """Карточки задач. Направление задано — только его задачи.

    Делить карту на файлы нельзя (ревью 12.09.2026: гейт потери задач увидел бы
    84 пропавших id, а валидатор схемы и сторожа зовут карту по точному имени).
    Механизм разделения в карте уже есть — `lines`, направления; ими и делим.
    """
    итог = [] if итог is None else итог
    if isinstance(узел, dict):
        # Направление: у него есть id и tasks, но нет status.
        if "id" in узел and "tasks" in узел and "status" not in узел:
            if направление and узел["id"] != направление:
                return итог
            for задача in узел.get("tasks") or []:
                карточки(задача, итог, направление)
            return итог
        if "id" in узел and "status" in узел:
            итог.append(узел)
        for значение in узел.values():
            карточки(значение, итог, направление)
    elif isinstance(узел, list):
        for значение in узел:
            карточки(значение, итог, направление)
    return итог


# Что вообще вправе стоять в поле `проверка.команда`. Список — ДАННЫЕ, и он
# узкий нарочно.
#
# Улика ревью кода 11.09.2026: команда шла в оболочку без единого
# ограничителя, а карточки пишет НЕ ЧЕЛОВЕК — ночной агент-строитель. То есть
# машина писала команду, машина её исполняла, и между ними не стоял никто:
# `git push`, `docker compose restart`, `psql -c "delete …"` прошли бы мимо
# И-1 и И-2 целиком (сторож команд видит только команды модели в сессии).
#
# Второй урок оттуда же: `echo ДОКАЗАНО` при `ждём: ДОКАЗАНО` — «доказано».
# Доказательство, которое печатает само себя, не доказательство; `echo` в
# список не входит и входить не может.
# Что вправе стоять в поле `проверка.команда`. Список ДАННЫЕ и узкий нарочно.
#
# Первая редакция была дырявой в трёх местах — ревью кода показало каждое:
#   • `[^;&|…]+` пропускает ПЕРЕВОД СТРОКИ, а `$` совпадает перед ним:
#     «test -s /etc/hostname\ntouch улика» проходило, и оболочка исполняла обе
#     команды. Отсюда `fullmatch` и запрет переводов строки;
#   • образец `bash scripts/*.sh` разрешал скрипт выката и скрипт публикации —
#     то есть выкат в прод и публикацию из cron, мимо И-2 и мимо слова
#     владельца. Отсюда: `bash scripts/…` только с `--selftest`;
#   • команда без `ждём` доказывалась ОДНИМ кодом возврата, а нулём кончается
#     почти всё. Требование непустого `ждём` стоит в `судить`.
# «..» стоит в запрете с 27.09.2026 (находка S-08 ревью безопасности): класс
# пути пропускал его, и форма была фильтром, а не песочницей — а агент-строитель
# вправе положить файл куда угодно и позвать его из карточки.
# Запрет разделён на две мерки, потому что у них разная ОБЛАСТЬ. Улика
# 15.10.2026: единый запрет искал опасные слова в ЛЮБОМ месте звена и
# отвергал все шесть законных проб ворот выката и установки — имя пробы
# делало её опасной. Любая задача про выкат или установку не могла быть
# закрыта машиной и висела в очереди владельца вечно.
# Разбиение по пути и операторы оболочки — по ЦЕЛОМУ звену: вырезать их
# нельзя, иначе выход за корень пройдёт как законный путь.
ЗАПРЕЩЕНО_В_ЗВЕНЕ = re.compile(r"\.\.|>|\||;|&|`|\$\(|\n|\r")
# Опасные ДЕЙСТВИЯ — по доводам, то есть по звену БЕЗ пути скрипта: сам
# путь уже судится образцами разрешённых форм, и быть гейтом или пробой
# он может только по имени `check-`/`test_`.
ЗАПРЕЩЕНО_В_ДОВОДАХ = re.compile(
    r"deploy|publikaciya|публикац|выкат|push|dropdb|docker|systemctl|rm\s|psql|migrat|миграц",
    re.I)
# Вырезается ТОЛЬКО путь пробы или гейта: имя `check-…`/`test_…` само говорит,
# чем файл является, и опасное слово в таком имени ничего не делает. У прочих
# форм имя не обещает ничего — вырезав его, я 15.10.2026 вернул в разрешённые
# `bash scripts/deploy.sh --selftest` и `bash scripts/install-docker.sh
# --selftest` (находка ревью безопасности, major): ключа `--selftest` у выката
# нет, доводы ушли бы в тело выката, а приёмка исполняет команду карточки сама,
# мимо сторожа команд.
ПУТЬ_СКРИПТА = re.compile(
    r"(?:scripts|harness/demons|harness/panel)/(?:check-|test_)[\w.-]+\.(?:py|sh)")


def запрещено(звено: str) -> bool:
    """Нарушает ли звено общий запрет: путь и операторы — целиком,
    действия — по доводам без имени скрипта."""
    return bool(ЗАПРЕЩЕНО_В_ЗВЕНЕ.search(звено)
                or ЗАПРЕЩЕНО_В_ДОВОДАХ.search(ПУТЬ_СКРИПТА.sub("", звено)))

БЕЗОПАСНЫЙ_ПУТЬ = r"[\w./-]+"
РАЗРЕШЁННЫЕ = (
    # Самотесты и гейты: единственная форма, которой можно что-то доказать.
    re.compile(rf"python3 scripts/{БЕЗОПАСНЫЙ_ПУТЬ}\.py( --selftest| --проба)"),
    # Без флага — ТОЛЬКО гейт или проба, и узнаются они по имени: в этом проекте
    # гейт зовётся `check-…`, проба `test_…`. Находка ревью безопасности
    # 27.09.2026 (critical): прежний шаблон «любой .py без доводов» впустил
    # `build-public.py`, а тот делает rsync --delete по боевому каталогу
    # публичной сборки — половина публикации без слова владельца, мимо И-2.
    # Живой прогон в 21:04 её выполнил, так что беда не воображаемая.
    re.compile(rf"python3 scripts/(check-|test_){БЕЗОПАСНЫЙ_ПУТЬ}\.py"),
    re.compile(rf"bash scripts/{БЕЗОПАСНЫЙ_ПУТЬ}\.sh --selftest"),
    # Скрипт, который ПО ИМЕНИ есть проба (`test_…`), флага не требует: он
    # проба целиком, `--selftest` у него нет. Без этой формы 27.09.2026
    # отвергались проверки вида `bash scripts/test_memory_commit.sh`, и задача
    # застревала в очереди владельца навсегда.
    re.compile(rf"bash scripts/test_{БЕЗОПАСНЫЙ_ПУТЬ}\.sh( --\S+)?"),
    re.compile(rf"bash harness/demons/{БЕЗОПАСНЫЙ_ПУТЬ}\.sh --selftest"),
    re.compile(rf"python3 harness/panel/test_{БЕЗОПАСНЫЙ_ПУТЬ}\.py"),
    # Чтение: файл на месте, строка в файле, живой ответ своей же машины.
    re.compile(rf"test -s {БЕЗОПАСНЫЙ_ПУТЬ}"),
    re.compile(rf"grep -c ('[^']*'|\"[^\"]*\"|\S+) {БЕЗОПАСНЫЙ_ПУТЬ}"),
    re.compile(r"curl -s http://127\.0\.0\.1[\w:/.?=-]*"),
    # Карточка тревоги сторожа проверяется ЕЁ ЖЕ формой: читающий вопрос
    # «ключ тревоги снова зелёный?». Ключ узкий (строчная латиница, цифры,
    # «:._-», до 80 знаков, первый знак буква или цифра): пустой, с пробелом,
    # «;» или «..» сюда не пройдёт, а пишущие флаги скрипта (--resolve,
    # --drain, --key) форме не соответствуют вовсе.
    re.compile(r"python3 scripts/alert-task\.py --is-green [a-z0-9][a-z0-9:._-]{0,79}"),
)


ФОРМЫ_СЛОВАМИ = (
    "python3 scripts/<имя>.py --selftest",
    "python3 scripts/check-<имя>.py               (гейт: без флага можно)",
    "python3 scripts/test_<имя>.py                (проба: без флага можно)",
    "bash scripts/<имя>.sh --selftest",
    "bash harness/demons/<имя>.sh --selftest",
    "python3 harness/panel/test_<имя>.py",
    "test -s <путь относительно корня проекта>",
    "grep -c '<образец>' <путь относительно корня проекта>",
    "curl -s http://127.0.0.1<адрес>",
    "bash scripts/test_<имя>.sh              (скрипт-проба, флаг не нужен)",
    "python3 scripts/alert-task.py --is-green <ключ тревоги>   (карточка тревоги сторожа)",
    "любые две-три формы выше через «&&»     (ждём тогда СПИСКОМ, по строке на звено)",
)


ЗВЕНЬЕВ_МАКСИМУМ = 3

# Поимённо разрешённые команды — ДАННЫЕ с причиной у каждой записи. Форма
# шаблоном покрывает не всё законное: гейт с читающим флагом, проба под
# scripts/hooks/, вариант самотеста. Шаблон вместо списка однажды впустил
# build-public.py с его rsync --delete по боевой сборке, поэтому здесь ТОЧНЫЕ
# строки, а не образцы.
def _путь_форм() -> Path:
    """Путь к данным о поимённых разрешениях. Окружение СТАРШЕ файла: иначе
    проба со своим списком судила бы боевой (та же грабля, что с конфигами)."""
    return Path(os.environ.get("ACCEPTANCE_FORMS")
                or КОРЕНЬ / "harness" / "config" / "acceptance-forms.yaml")


def _местные_формы() -> Path:
    """Разрешения ЭТОЙ установки: команды, называющие продукт.

    Отдельный файл, и не в пакете: общие данные уезжают в публичную сборку
    харнеса, а имя и пути продукта владельца наружу не едут (его слово
    13.10.2026). Файла нет — разрешений продукта просто нет.
    """
    return КОРЕНЬ / "acceptance-forms-local.yaml"


def поимённо(путь: Path | None = None) -> set:
    """Команды из данных, у которых НАЗВАНА причина. Общие плюс местные.

    Нечитаемый файл — пустое множество: закрыто, а не «разрешено всё». Запись
    без причины не действует (та же дисциплина, что у host-independence.yaml).
    """
    если_общие = путь is None
    путь = путь or _путь_форм()
    # yaml ввозится ЗДЕСЬ: у приёмки он нужен только этому месту, а без него
    # механизм обязан работать (и тогда поимённых разрешений просто нет).
    try:
        import yaml
    except ImportError:
        return set()
    try:
        данные = yaml.safe_load(путь.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    итог = set()
    for запись in (данные.get("по_имени") or []):
        if not isinstance(запись, dict):
            continue
        команда = str(запись.get("команда") or "").strip()
        причина = str(запись.get("причина") or "").strip()
        if команда and причина:
            итог.add(команда)
    # Местные разрешения читаются ТОЛЬКО когда спрашивают боевой список: проба
    # со своим файлом судит свой список, иначе стенд играл бы на производстве.
    if если_общие and _местные_формы().exists():
        итог |= поимённо(_местные_формы())
    return итог


def позволена(команда: str) -> bool:
    """Годится ли команда для приёмки. Судит ФОРМА, а не намерение автора.

    Судить намерение нельзя: карточки пишет ночной агент, и «команда обещала
    не менять состояние» — это текст в промпте, а не носитель.

    Цепочка `A && B` судится ПО ЗВЕНЬЯМ: каждое обязано быть разрешённой формой
    и пройти общий запрет. Строгость от этого не падает — одиночный `&` внутри
    звена по-прежнему запрещён, — а 18 задач 27.09.2026 перестают залегать
    между мерками: «два самотеста через &&» стали законной формой.
    """
    звенья = [звено.strip() for звено in команда.split("&&")]
    if not (1 <= len(звенья) <= ЗВЕНЬЕВ_МАКСИМУМ):
        return False
    названные = поимённо()
    return all(звено and (звено in названные
                          or (not запрещено(звено)
                              and any(обр.fullmatch(звено) for обр in РАЗРЕШЁННЫЕ)))
               for звено in звенья)


def прогнать(команда: str, корень: Path, потолок_звена: int | None = None) -> tuple[int, str]:
    """Команда проверки. Своей оболочки у неё нет: что написано, то и идёт.

    Потолок считается ПО ЗВЕНЬЯМ: цепочка из трёх честных самотестов делила бы
    один потолок на троих и падала по времени без вины. Процесс запускается
    своей группой, и на таймауте убивается ГРУППА: иначе оболочка умирает, а
    работающее звено остаётся сиротой и продолжает править файлы без присмотра
    (находка ревью безопасности 27.09.2026).

    `потолок_звена` нужен ПРОБАМ таймаута: с боевыми 120 с они спали 367 секунд
    на каждый прогон самотеста, а полные ворота зовут его дважды (гейт и
    самотест сторожа приёмки) — двенадцать минут сна за прогон. Поведение
    доказывается тем же кодом при потолке в секунды; бой берёт своё умолчание.
    """
    потолок = (потолок_звена or ПОТОЛОК_СЕК) * max(1, len(команда.split("&&")))
    try:
        проц = subprocess.Popen(команда, shell=True, cwd=str(корень),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace",
                                start_new_session=True)
    except OSError as беда:
        return 1, f"проверка не запустилась: {беда}"
    try:
        вывод, ошибки = проц.communicate(timeout=потолок)
        return проц.returncode, вывод + ошибки
    except subprocess.TimeoutExpired:
        # Не уложилась в потолок — это НЕ провал проверки: механизм может быть
        # исправен, а команда просто длинная. Провал поднимает задачу на
        # вершину очереди как «сломалось то, что считалось сделанным».
        try:
            os.killpg(os.getpgid(проц.pid), signal.SIGKILL)
        except OSError:
            проц.kill()
        проц.communicate()
        return -1, f"проверка не уложилась в {потолок} с"
    # Вывод отдаётся ЦЕЛИКОМ: обрезка до сравнения выкидывала итоговую строку.
    # Улика 12.09.2026: у test_guard_bash.py итог «неудач: 0» печатается в
    # stdout, а следом склеивается 89 КБ stderr — последние 4000 знаков были
    # хвостом stderr, и исправный механизм получил «провалила проверку».
    return готово.returncode, готово.stdout + готово.stderr


def судить(задача: dict, корень: Path) -> dict:
    """Вердикт по одной задаче.

    Над `_судить` стоит один переход: «доказать нечем» у задачи, про которую
    строитель уже сказал «может только владелец», — это НЕ долг механизма, а
    очередь владельца, и число у неё своё. Пока оба состояния были одним
    исходом, его очередь была неотличима от неразобранного (находка C5 ревью
    кода 11.09.2026, закрытая тогда наполовину) — и на вопрос «надо именно мне
    их все проверять?» ответить числом было нечем.

    Переход ОДИН и на выходе: пометка смотрится только там, где доказать
    действительно нечем, поэтому годная проверка закрывает задачу и с пометкой.
    """
    вердикт = _судить(задача, корень)
    # `owner_testable: true` — прямая пометка «проверяет владелец», и она
    # значит ровно то же, что «проверить_владельцу»: задача ждёт ЕГО, а не
    # достройки механизма. Без этого задача с человеческой проверкой числилась
    # долгом СТРОИТЕЛЯ и попадала в список «доказать нечем» (замер 28.09.2026:
    # так висели публикация харнеса и ещё три карточки).
    почему_владельцу = задача.get("проверить_владельцу") or (
        задача.get("как_проверить") if задача.get("owner_testable") is True
        else None)
    if вердикт["исход"] == "доказать-нечем" and почему_владельцу:
        return {**вердикт, "исход": "ждёт-владельца",
                "почему": f"доказать может только владелец: {почему_владельцу}"}
    if вердикт["исход"] == "доказано":
        долг = долг_мутаций_задачи(задача, корень)
        if долг:
            return {**вердикт, "исход": "ждёт-мутаций", "почему": долг}
    return вердикт


# ── Мутации — шаг задачи до test (пульт pipeline.yaml, шаги; ключ «шаги_приёмки» — ручка) ──
ИТОГ_ПРИБОРА = re.compile(r"покраснели как надо (\d+), дыр (\d+)(?:, из кэша (\d+))?")
НЕДОГОН_ПРИБОРА = re.compile(r"НЕДОГНАНА: не прогнано (\d+) из")
ТАБЛИЦЫ_МУТАЦИЙ = ("harness/config/probe-mutations.yaml", "harness/config/invariants.yaml")
ПРИБОР_МУТАЦИЙ = ["bash", "scripts/mutation-probe.sh"]
_ПАЙПЛАЙН = []


def шаги_приёмки_карточки(задача: dict, корень: Path):
    """Шаги, отложенные пайплайном до приёмки; None — история не прочитана.

    Судья один — гейт пайплайна. Раскладка без него (стенд демона) пайплайна
    не ведёт, и требовать его шагов там некому.
    """
    if not _ПАЙПЛАЙН:
        путь = Path(__file__).resolve().parent / "check-pipeline.py"
        if not путь.is_file():
            return []
        import importlib.util
        спец = importlib.util.spec_from_file_location("check_pipeline", путь)
        модуль = importlib.util.module_from_spec(спец)
        спец.loader.exec_module(модуль)
        _ПАЙПЛАЙН.append(модуль)
    return _ПАЙПЛАЙН[0].шаги_приёмки_задачи(задача, корень)


def журнал_мутаций(корень: Path, ид: str) -> Path:
    return корень / ".tasks" / ид / "мутации.log"


GIT_TRIES = 3
GIT_TIMEOUT_SEC = 120
# Запись ответов git режима --история: ключ «дерево + доводы git» → строки или None.
_recorded: dict | None = None
_slice_answers: dict | None = None


class HistoryUnread(Exception):
    """История git не прочитана: git молчал все попытки или без неё судить нечем."""


def _event(name: str, outcome: str, **data) -> None:
    events.emit("scripts/acceptance.py", name, outcome, journal="checks-slice", data=data)


def _history_key(корень: Path, args: tuple) -> str:
    return json.dumps([str(Path(корень).resolve()), *args], ensure_ascii=False)


def _answers_from_file() -> dict | None:
    """Ответы git из файла истории, который записал исполнитель вне среза: в срезе git
    ждёт память, и каждый его вызов — таймаут. Вне среза файл не читается."""
    global _slice_answers
    path = os.environ.get("HARNESS_GIT_HISTORY")
    if not path or not os.environ.get("HARNESS_IN_CHECKS_SLICE"):
        return None
    if _slice_answers is None:
        _slice_answers = json.loads(Path(path).read_text(encoding="utf-8"))
    return _slice_answers


def _git_answer(корень: Path, args: tuple) -> list[str] | None:
    """Живой git. Повторяется только молчание — таймаут и отказ запуска; код не 0 —
    ответ (merge-base --is-ancestor кодом 1 отвечает «не предок»), его не повторяют."""
    reason = ""
    for _ in range(GIT_TRIES):
        try:
            done = subprocess.run(["git", "-C", str(корень), "-c", "core.quotepath=false", *args],
                                  capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            reason = f"таймаут {GIT_TIMEOUT_SEC} с"
        except OSError as error:
            reason = f"{type(error).__name__}: {error}"
        else:
            return done.stdout.splitlines() if done.returncode == 0 else None
    if _recorded is not None:
        raise HistoryUnread(f"git {' '.join(args[:3])}: {reason}")
    return None


def _git_lines(корень: Path, *args: str) -> list[str] | None:
    """Строки вывода git; None — git ответил отказом или молчит. В срезе с файлом
    истории ответ берётся из файла; ключа нет — живой git и событие."""
    key = _history_key(корень, args)
    answers = _answers_from_file()
    if answers is not None:
        if key in answers:
            return answers[key]
        _event("mutations.history-miss", "held", ключ=key)
    answer = _git_answer(корень, args)
    if _recorded is not None:
        _recorded[key] = answer
    return answer


def mutation_table(корень: Path) -> tuple[set, dict, dict]:
    """(механизмы с записями мутаций, файл-доказательство -> его механизмы,
    то же по записям без якоря).

    Поле «механизм» — файл или список: проба судит несколько файлов
    (test_limit_silence.sh — четыре), и её правка — шаг каждого из них.
    Якорь `x.sh#часть` — доказательство в части файла: правка файла вне неё
    механизм не задевает, поэтому шаг задачи тянут только записи без якоря,
    а свежесть бумаги судится по всем."""
    import yaml

    def load(name: str, key: str):
        путь = корень / "harness" / "config" / name
        if not путь.is_file():
            return None
        return (yaml.safe_load(путь.read_text(encoding="utf-8")) or {}).get(key)

    invariants = [e for e in load("invariants.yaml", "инварианты") or [] if e.get("мутация")]
    proofs: dict = {}
    whole: dict = {}
    for п, з in (load("probe-mutations.yaml", "пробы") or {}).items():
        if з and з.get("мутация"):
            мм = dict.fromkeys(з["механизм"] if isinstance(з["механизм"], list) else [з["механизм"]])
            proofs.setdefault(proof_path(п), {}).update(мм)
            if "#" not in п:
                whole.setdefault(proof_path(п), {}).update(мм)
    proofs, whole = ({п: tuple(мм) for п, мм in т.items()} for т in (proofs, whole))
    return {e.get("файл") for e in invariants} | {м for мм in proofs.values() for м in мм}, proofs, whole


def proof_path(ключ: str) -> str:
    """Файл пробы под ключом таблицы мутаций. Ключ YAML уникален, поэтому вторая
    запись того же файла пишется `scripts/./x.py` или `x.py#якорь`; git и история
    путей знают только сам файл, и ключ как путь давал ложное «бумага судила
    другой код» и слепоту к правке пробы."""
    return os.path.normpath(ключ.split("#", 1)[0])


def механизмы_задачи(ид: str, корень: Path) -> list[str] | None:
    """Механизмы с записями мутаций, которых коснулась задача: сами или их
    файл-доказательство. None — история не прочитана.

    Файл судится, пока в нём живы строки задачи: старый коммит с трейлером, чьи
    строки давно переписала другая задача, иначе тянул в шаг чужие механизмы на
    часы (06.11: 6fb1a932 — session-warden.sh в шаге hr.prikaz-vykata-vopros-i-na-potom)."""
    файлы = файлы_задачи(ид, корень)
    if файлы is None:
        return None
    mechanisms, _, proofs = mutation_table(корень)
    файлы = [ф for ф in файлы
             if (ф in mechanisms or ф in proofs) and task_changed_lines(ид, корень, ф)]
    return sorted({ф for ф in файлы if ф in mechanisms}
                  | {м for ф in файлы if ф in proofs for м in proofs[ф]})


def paper_sections(текст: str, mechanisms: list[str]) -> dict[str, str]:
    """Разделы `--- <механизм>` бумаги; раздел кончается следующим разделом или итогом."""
    sections, current = {}, None
    for строка in текст.splitlines():
        if строка.startswith("=== мутации задачи"):
            current = None
        elif строка.startswith("--- ") and строка[4:] in mechanisms:
            current = строка[4:]
            sections[current] = []
        elif current:
            sections[current].append(строка)
    return {к: "\n".join(в) for к, в in sections.items()}


def section_debt(тело: str) -> str | None:
    """Почему раздел одного механизма не зелёный; None — зелёный."""
    итоги = ИТОГ_ПРИБОРА.findall(тело)
    if not итоги:
        return "в разделе нет итога прибора (прибор упал?)"
    if int(итоги[-1][1]):
        return f"дыр {итоги[-1][1]}"
    return "прогон НЕДОГНАН" if "НЕДОГНАНА" in тело else None


def stale_debts(ид: str, корень: Path, sha: str, mechanisms: list[str]) -> list[str]:
    """Правки задачи после sha бумаги в механизмы, их доказательства и таблицы."""
    _, proofs, _ = mutation_table(корень)
    watched = {м: м for м in mechanisms}
    watched.update({п: м for п, мм in proofs.items() for м in мм if м in mechanisms})
    watched.update({т: т for т in ТАБЛИЦЫ_МУТАЦИЙ})
    после = _git_lines(корень, "log", "--format=", "--name-only",
                       f"--grep=^Dev-Map: {ид}$", "--extended-regexp", f"{sha}..HEAD")
    if после is None:
        return ["бумага мутаций: коммиты после неё не прочитаны"]
    return [f"бумага старше правки: после коммита {sha[:9]} задача правила {ф} ({watched[ф]})"
            for ф in dict.fromkeys(после) if ф in watched]


def judged_entries(корень: Path, rev: str, mechanisms: list[str]) -> dict:
    """Записи таблиц мутаций о механизмах в ревизии rev: по ним бумага выбирала мутации."""
    import yaml
    wanted = set(mechanisms)

    def table(name: str, key: str):
        return (yaml.safe_load("\n".join(_git_lines(корень, "show", f"{rev}:{name}") or [])) or {}).get(key)

    probes, invariants = ТАБЛИЦЫ_МУТАЦИЙ
    return {probes: {п: з for п, з in (table(probes, "пробы") or {}).items()
                     if з and wanted & set(з["механизм"] if isinstance(з["механизм"], list)
                                           else [з["механизм"]])},
            invariants: [e for e in table(invariants, "инварианты") or [] if e.get("файл") in wanted]}


def _same_blob(корень: Path, sha: str, путь: str) -> bool:
    then = _git_lines(корень, "rev-parse", f"{sha}:{путь}")
    return then is not None and then == _git_lines(корень, "rev-parse", f"HEAD:{путь}")


def paper_code_debts(корень: Path, sha: str, mechanisms: list[str]) -> list[str]:
    """Бумага вне истории HEAD годна, если судила тот же код: механизмы, их
    доказательства и их записи мутаций в sha те же, что в HEAD. Очередь слияния
    перебазирует ветку — sha меняются, содержимое нет; сверка содержимого
    переживает и rebase, и squash, а сверка sha валила неизменный код (22.11)."""
    if _git_lines(корень, "cat-file", "-e", f"{sha}^{{commit}}") is None:
        return [f"мутации: коммит бумаги {sha[:9]} не найден в репозитории"]
    _, proofs, _ = mutation_table(корень)
    files = list(mechanisms) + sorted(п for п, мм in proofs.items() if set(мм) & set(mechanisms))
    debts = [f"мутации: бумага судила другой код: {ф} изменился после коммита бумаги {sha[:9]}"
             for ф in dict.fromkeys(files) if not _same_blob(корень, sha, ф)]
    then, now = (judged_entries(корень, rev, mechanisms) for rev in (sha, "HEAD"))
    return debts + [f"мутации: бумага судила другие записи мутаций: {т} изменилась после "
                    f"коммита бумаги {sha[:9]}" for т in ТАБЛИЦЫ_МУТАЦИЙ if then[т] != now[т]]


def mutation_paper_debts(ид: str, корень: Path) -> list[str] | None:
    """Долги шага «мутации»: пусто — шаг пройден или не требуется; None — история
    не прочитана. Судит бумагу по коммиту, а не по времени файла."""
    mechanisms = механизмы_задачи(ид, корень)
    if not mechanisms:
        return mechanisms
    журнал = журнал_мутаций(корень, ид)
    if not журнал.is_file():
        return [f"мутации: нет .tasks/{ид}/мутации.log; механизмы: {', '.join(mechanisms)}"]
    текст = журнал.read_text(encoding="utf-8", errors="replace")
    шапка = re.match(r"коммит ([0-9a-f]{7,40})$", текст.splitlines()[0] if текст else "")
    if not шапка:
        return ["мутации: в шапке бумаги нет «коммит <sha>»"]
    sha = шапка.group(1)
    if _git_lines(корень, "merge-base", "--is-ancestor", sha, "HEAD") is None:
        since = paper_code_debts(корень, sha, mechanisms)
    else:
        since = stale_debts(ид, корень, sha, mechanisms)
    sections = paper_sections(текст, mechanisms)
    debts = []
    for м in mechanisms:
        беда = section_debt(sections[м]) if м in sections else "нет раздела в бумаге"
        if беда:
            debts.append(f"мутации: {м}: {беда}")
    return debts + since


def долг_мутаций(ид: str, корень: Path, отложенные) -> str | None:
    """Почему мутации задачи не закрыты; None — долга нет (или шаг не отложен)."""
    if "мутации" not in (отложенные or ()):
        return None
    debts = mutation_paper_debts(ид, корень)
    if debts is None:
        return "мутации: история задачи не прочитана (git молчит)"
    return "; ".join(debts) or None


def _хакатон():
    import importlib.util
    спец = importlib.util.spec_from_file_location(
        "hackathon_acceptance", Path(__file__).resolve().parent / "hackathon.py")
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


def долг_мутаций_задачи(задача: dict, корень: Path) -> str | None:
    """Долг мутаций карточки. История задачи — в репозитории харнеса (КОРЕНЬ),
    журнал — там, где судятся её проверки (корень)."""
    ид = задача.get("id", "?")
    if _хакатон().is_hackathon(задача):
        return None  # мутации хакатона — долг карточки <id>-prod-debt
    отложенные = шаги_приёмки_карточки(задача, КОРЕНЬ)
    if отложенные is None:
        return "мутации: история задачи не прочитана (git молчит) — долг не судим"
    if "мутации" not in отложенные:
        return None
    return долг_мутаций(ид, корень, отложенные)


def файлы_задачи(ид: str, корень: Path) -> list[str] | None:
    """Существующие файлы из коммитов задачи; None — git молчит."""
    имена = _git_lines(корень, "log", "--format=", "--name-only",
                       f"--grep=^Dev-Map: {ид}$", "--extended-regexp")
    if имена is None:
        return None
    return [и for и in dict.fromkeys(с for с in имена if с.strip()) if (корень / и).is_file()]


def task_changed_lines(ид: str, корень: Path, файл: str) -> str | None:
    """Номера строк файла (как в рабочем дереве), изменённых задачей, через запятую.
    None — сужать нельзя: файл задача не правила, база не найдена или строк нет."""
    commits = _git_lines(корень, "log", "--reverse", "--format=%H", f"--grep=^Dev-Map: {ид}$",
                         "--extended-regexp", "--", файл)
    if not commits:
        return None
    diff_lines = _git_lines(корень, "diff", "-U0", f"{commits[0]}^", "--", файл)
    if not diff_lines:
        return None
    # Разница от первого коммита задачи до дерева несёт и правки ДРУГИХ задач,
    # слитые позже в тот же файл: шаг судил задаче чужие строки (16.11:
    # задача по замеру — 35 из 41 строки реестра журналов). Строка остаётся
    # задаче, если blame называет её коммит или правку в работе.
    чьи = _blame_commits(корень, файл)
    своё = set(commits) | {UNCOMMITTED}
    lines = {n for _, _, start, count in map(_hunk, diff_lines) if count
             for n in range(start, start + count) if чьи.get(n, UNCOMMITTED) in своё}
    lines |= _own_deletion_seams(корень, файл, commits[0], своё)
    lines -= _silent_lines(корень, файл)
    return ",".join(map(str, sorted(lines))) or None


HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunk(line: str) -> tuple[int, int, int, int]:
    """(старое начало, старых строк, новое начало, новых строк); не стык — нули."""
    found = HUNK.match(line)
    if not found:
        return 0, 0, 0, 0
    old_start, old_count, new_start, new_count = found.groups()
    return int(old_start), int(old_count or 1), int(new_start), int(new_count or 1)


def _own_deletion_seams(корень: Path, файл: str, base: str, своё: set) -> set[int]:
    """Строки стыков чистых удалений, сделанных задачей (стык N — строки N и N+1).
    blame удаления не видит, а разница от базы не знает, ЧЕЙ коммит удалял:
    стык чужого удаления уходил задаче (16.11: owner_word.py 37–38 — удалял
    6bc44771). Поэтому идём по коммитам по порядку, сдвигая стыки задачи
    правками после них; правка в работе — последний шаг, она своя."""
    history = _git_lines(корень, "log", "-p", "-U0", "--reverse", "--format=COMMIT %H",
                         f"{base}^..HEAD", "--", файл) or []
    work = _git_lines(корень, "diff", "-U0", "HEAD", "--", файл) or []
    steps: list[tuple[str, list]] = []
    for line in history:
        if line.startswith("COMMIT "):
            steps.append((line.split()[1], []))
        elif steps and line.startswith("@@"):
            steps[-1][1].append(_hunk(line))
    steps.append((UNCOMMITTED, [_hunk(line) for line in work if line.startswith("@@")]))
    seams: set[int] = set()
    for sha, hunks in steps:
        seams = {moved for seam in seams if (moved := _shift_seam(seam, hunks)) is not None}
        if sha in своё:
            seams |= {new_start for _, old_count, new_start, new_count in hunks
                      if old_count and not new_count}
    return {n for seam in seams for n in (seam, seam + 1) if n > 0}


def _shift_seam(seam: int, hunks: list) -> int | None:
    """Где стык после строки seam после правки; None — правка заменила его соседей."""
    shift = 0
    for old_start, old_count, _, new_count in hunks:
        # Вставка (old_count 0) идёт ПОСЛЕ строки old_start.
        last = old_start + old_count - 1 if old_count else old_start
        if last <= seam:
            shift += new_count - old_count
        elif old_start <= seam:
            return None
    return seam + shift


SILENT_SUFFIXES = (".sh", ".py", ".yaml", ".yml")


def _silent_lines(корень: Path, файл: str) -> set[int]:
    """Пустые строки и комментарии: поведения не несут, мутировать в них нечего
    (16.11: pre-commit-gate.sh 748 — комментарий, 753 — пустая)."""
    if not файл.endswith(SILENT_SUFFIXES):
        return set()
    try:
        text = (корень / файл).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {n for n, line in enumerate(text.splitlines(), 1)
            if not line.strip() or line.lstrip().startswith("#")}


UNCOMMITTED = "0" * 40


def _blame_commits(корень: Path, файл: str) -> dict[int, str]:
    """Номер строки рабочего дерева -> коммит, что её последним менял (нули — в работе)."""
    чьи: dict[int, str] = {}
    for line in _git_lines(корень, "blame", "--line-porcelain", "--", файл) or []:
        head = re.match(r"([0-9a-f]{40}) \d+ (\d+)", line)
        if head:
            чьи[int(head.group(2))] = head.group(1)
    return чьи


class QueueBusy(Exception):
    """Очередь среза занята дольше предела ожидания: прогон не состоялся."""


QUEUE_BUSY_RC = 75


def мутации_задачи(ид: str, корень: Path, прибор: list[str], narrow: bool = True) -> int | None:
    """Прогнать мутации механизмов задачи в её бумагу. Дыр N; None — механизмов
    нет или история не прочитана (бумага тогда не пишется). Срок захода вышел
    до конца задачи — QueueBusy: прогон не состоялся, его повторят."""
    try:
        итог = мутации_пачки([ид], корень, прибор, narrow)
    except HistoryUnread:
        return None
    if ид not in итог:
        raise QueueBusy("срок захода вышел раньше, чем механизмы задачи догнаны")
    return итог[ид]


def _batch_lines(ids: list[str], корень: Path, файл: str) -> str | None:
    """Строки механизма для прогона пачки: объединение строк её задач. None — хоть
    одна задача сузить не даёт, механизм гонится целиком."""
    lines: set[int] = set()
    for ид in ids:
        own = task_changed_lines(ид, корень, файл)
        if own is None:
            return None
        lines |= {int(n) for n in own.split(",")}
    return ",".join(map(str, sorted(lines)))


ИСТОРИЯ_МЕХАНИЗМОВ = "мутации-механизмов.tsv"


def _history_path() -> Path:
    return Path(конф.log_dir()) / ИСТОРИЯ_МЕХАНИЗМОВ


def mechanism_estimates() -> dict[str, float]:
    """Оценка длительности механизма, с: медиана пяти последних прогонов из
    истории. Механизм без истории — медиана всех известных (нет истории — 0)."""
    import statistics
    прогоны: dict[str, list[float]] = {}
    try:
        строки = _history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        строки = []
    for строка in строки:
        поля = строка.split("\t")
        try:
            прогоны.setdefault(поля[1], []).append(float(поля[2]))
        except (IndexError, ValueError):
            continue
    return {м: statistics.median(с[-5:]) for м, с in прогоны.items()}


def _record_mechanism(файл: str, секунд: float, покраснели: int, дыр: int) -> None:
    """Строка истории ПО ГОТОВНОСТИ механизма: она же оценка для плана захода."""
    путь = _history_path()
    try:
        путь.parent.mkdir(parents=True, exist_ok=True)
        with путь.open("a", encoding="utf-8") as fh:
            fh.write(f"{int(time.time())}\t{файл}\t{секунд:.0f}\t{покраснели}\t{дыр}\n")
    except OSError as error:
        print(f"мутации: история длительностей не записана: {error}", flush=True)


def _run_deadline() -> float | None:
    """Мягкий срок захода (эпоха) из MUTATION_DEADLINE_TS: его ставит дверь среза
    до предела юнита с запасом. Пусто — без срока (ручной запуск вне двери)."""
    try:
        return float(os.environ["MUTATION_DEADLINE_TS"])
    except (KeyError, ValueError):
        return None


def batch_plan(по_задаче: dict[str, list[str] | None], оценки: dict[str, float]) -> list[str]:
    """Порядок механизмов захода: задачи — от дешёвой к дорогой по оценке, механизмы
    — задачей подряд. Обрыв или срок тогда застают больше готовых бумаг: одна
    задача на десятки механизмов иначе держит за собой все малые."""
    известные = sorted(оценки.values())
    # Без истории цена задачи — число её механизмов, а не ноль у всех.
    обычная = известные[len(известные) // 2] if известные else 1.0

    def цена(mechanisms: list[str]) -> float:
        return sum(оценки.get(м, обычная) for м in mechanisms)

    порядок: list[str] = []
    for ид in sorted((и for и in по_задаче if по_задаче[и]), key=lambda и: цена(по_задаче[и])):
        порядок += [м for м in по_задаче[ид] if м not in порядок]
    return порядок


def мутации_пачки(ids: list[str], корень: Path, прибор: list[str],
                  narrow: bool = True) -> dict[str, int | None]:
    """Мутации пачки задач: общий механизм гонится ОДИН раз (по строкам всех её
    задач), бумага каждой задачи — только её механизмы. {id: дыр | None}.

    Бумага задачи пишется, КАК ТОЛЬКО догнаны её механизмы, а строка механизма с
    длительностью печатается сразу: обрыв захода по пределу юнита оставляет
    готовое и виден в журнале. После мягкого срока новый механизм не начинается;
    задачи с недогнанными механизмами в ответ не входят: о каждой печатается,
    сколько мутаций заход прогнал по-новой, — по этому числу очередь решает, тратить
    ли заявке попытку. Добьёт её следующий заход, сделанные мутации отдаст кэш прибора."""
    по_задаче = {ид: механизмы_задачи(ид, корень) for ид in ids}
    head = _git_lines(корень, "rev-parse", "HEAD")
    if not head:
        raise HistoryUnread("git rev-parse HEAD не ответил")
    итог: dict[str, int | None] = {ид: None for ид, m in по_задаче.items() if not m}
    оценки = mechanism_estimates()
    чьи = {м: [ид for ид in ids if м in (по_задаче[ид] or ())] for м in batch_plan(по_задаче, оценки)}
    срок = _run_deadline()
    print(f"мутации: заход {len(чьи)} механизмов, оценка "
          f"{sum(оценки.get(м, 0) for м in чьи) / 60:.0f} мин"
          + (f", срок {time.strftime('%H:%M', time.localtime(срок))}" if срок else ""), flush=True)
    прогоны = {}
    новых: dict[str, int] = {}
    for файл, owners in чьи.items():
        if срок and time.time() >= срок:
            print(f"мутации: срок захода вышел — не начато механизмов {len(чьи) - len(прогоны)}",
                  flush=True)
            break
        # Шаг задачи гоняет мутации ИЗМЕНЁННЫХ задачей строк; полный набор — ночью.
        env = dict(os.environ)
        lines = _batch_lines(owners, корень, файл) if narrow else None
        if lines:
            env["MUT_ONLY_LINES"] = lines
        else:
            env.pop("MUT_ONLY_LINES", None)
        # Файл истории — для захода; самотесты копий прибора во временных репо он бы сбил.
        env.pop("HARNESS_GIT_HISTORY", None)
        начало = time.monotonic()
        ответ = subprocess.run(прибор + ["--механизм", файл], cwd=str(корень), env=env,
                               capture_output=True, text=True, errors="replace")
        секунд = time.monotonic() - начало
        if ответ.returncode == QUEUE_BUSY_RC:
            raise QueueBusy((ответ.stdout + ответ.stderr).strip())
        вывод = ответ.stdout + ответ.stderr
        for line in вывод.splitlines():
            if "правка без мутаций" in line:
                print(line.strip(), flush=True)
        итог_прибора = ИТОГ_ПРИБОРА.findall(вывод)
        # Прибор без итога ничего не доказал — это дыра, а не тишина.
        покраснели, дыр, кэш = (tuple(int(x or 0) for x in итог_прибора[-1])
                                if итог_прибора else (0, 1, 0))
        недогон = НЕДОГОН_ПРИБОРА.findall(вывод)
        прогоны[файл] = (вывод, покраснели, дыр)
        # Дыра «без итога» — не ход: засчитай её, и сломанный прибор крутил бы заявку
        # в очереди без траты попыток.
        новых[файл] = покраснели + дыр - кэш if итог_прибора else 0
        print(f"механизм {len(прогоны)}/{len(чьи)} {файл}: {секунд:.0f} с, покраснели {покраснели}, "
              f"дыр {дыр}, из кэша {кэш}" + (f", не прогнано {недогон[-1]}" if недогон else "")
              + f" (задачи {','.join(owners)})", flush=True)
        _record_mechanism(файл, секунд, покраснели, дыр)
        for ид in owners:
            mechanisms = по_задаче[ид]
            if ид not in итог and all(м in прогоны for м in mechanisms):
                итог[ид] = _write_paper(ид, корень, head[0], {м: прогоны[м] for м in mechanisms})
                print(f"мутации задачи {ид}: дыр {итог[ид]}, бумага {журнал_мутаций(корень, ид)}",
                      flush=True)
    for ид in ids:
        if ид not in итог:
            сдвиг = sum(новых[м] for м in по_задаче[ид] if м in новых)
            print(f"мутации задачи {ид}: не догнана, новых мутаций {сдвиг} — срок захода вышел; "
                  "подай её снова, готовые мутации отдаст кэш прибора", flush=True)
    return итог


def _write_paper(ид: str, корень: Path, head: str, прогоны: dict) -> int:
    """Бумага задачи из прогонов её механизмов; отдаёт дыр."""
    покраснели = sum(к for _, к, _ in прогоны.values())
    дыр = sum(д for _, _, д in прогоны.values())
    куски = [f"--- {файл}\n{вывод}" for файл, (вывод, _, _) in прогоны.items()]
    итог = f"=== мутации задачи {ид}: покраснели {покраснели}, дыр {дыр} ==="
    журнал = журнал_мутаций(корень, ид)
    журнал.parent.mkdir(parents=True, exist_ok=True)
    журнал.write_text("\n".join([f"коммит {head}"] + куски + [итог]) + "\n", encoding="utf-8")
    return дыр


def ночные_мутации(карта: dict, корень: Path, прибор: list[str], писатель) -> list:
    """Ночь: мутации задач `test` с долгом. Дыр 0 гасит карточку слабой пробы,
    дыры или их отсутствие заводят её. Отдаёт [(id, дыр | None)]."""
    import hashlib
    итог = []
    for з in карточки(карта):
        if з.get("status") != "test":
            continue
        долг = долг_мутаций_задачи(з, корень)
        if not долг or "не прочитана" in долг:
            continue
        ид = з.get("id", "?")
        try:
            дыр = мутации_задачи(ид, корень, прибор, narrow=False)
        except QueueBusy:
            continue  # очередь занята: ночь повторит, а не заводит карточку «слабая проба»
        ключ = "task-mutations:" + hashlib.sha1(ид.encode()).hexdigest()[:12]
        if дыр == 0:
            писатель(["--resolve", ключ])
        else:
            беда = f"дыр {дыр}" if дыр else "мутаций для её механизмов нет"
            писатель(["--key", ключ, "--title", f"Проба слабая: мутации задачи {ид} — {беда}",
                      "--text", f"Журнал .tasks/{ид}/мутации.log. Приёмка не примет задачу, "
                                "пока мутации механизмов задачи не покраснеют все."])
        итог.append((ид, дыр))
    return итог


def _mutation_probe() -> list[str]:
    import shlex
    return shlex.split(os.environ.get("HARNESS_MUTATION_PROBE") or "") or ПРИБОР_МУТАЦИЙ


def _task_mutations(ids: list[str]) -> int:
    """Шаг «мутации» пачки задач: бумага .tasks/<id>/мутации.log каждой. Код 0 — дыр 0,
    1 — дыры, 2 — история не прочитана. Тревог не заводит: дыру чинит тот же исполнитель."""
    unread = [ид for ид in ids if механизмы_задачи(ид, КОРЕНЬ) is None]
    for ид in unread:
        print(f"мутации задачи {ид}: история не прочитана (git молчит)", flush=True)
    # Повтор после обрыва берёт только недоделанное: свежая зелёная бумага —
    # результат, перегонять её значит тратить срез заново.
    fresh = [ид for ид in ids if ид not in unread and механизмы_задачи(ид, КОРЕНЬ)
             and mutation_paper_debts(ид, КОРЕНЬ) == []]
    for ид in fresh:
        print(f"мутации задачи {ид}: дыр 0, бумага {журнал_мутаций(КОРЕНЬ, ид)} "
              "(свежая и зелёная — не перегонялась)", flush=True)
    # Пачку собирает очередь из заявок разных помощников: чужая нечитаемая
    # задача не отнимает прогон у остальных.
    ids = [ид for ид in ids if ид not in unread and ид not in fresh]
    try:
        итог = мутации_пачки(ids, КОРЕНЬ, _mutation_probe()) if ids else {}
    except QueueBusy as busy:
        print(f"мутации задач {','.join(ids)}: не прогнано: очередь среза занята — {busy}", flush=True)
        return QUEUE_BUSY_RC
    except HistoryUnread as error:
        for ид in ids:
            print(f"мутации задачи {ид}: история не прочитана ({error})", flush=True)
        return 2
    for ид, дыр in итог.items():
        if дыр is None:
            print(f"мутации задачи {ид}: механизмов с записями нет — шаг пуст", flush=True)
    short = [ид for ид in ids if ид not in итог]
    return 2 if unread or short else 1 if any(итог.values()) else 0


# Закон пачек (слово владельца 14.11): ключи — pipeline.yaml → пачка, умолчания — в очереди.
ИТОГ_ЗАДАЧИ = re.compile(r"^мутации задачи (\S+): (.*)$")


def _batch_settings() -> dict:
    import batch_queue
    return batch_queue.settings(КОРЕНЬ)


def _own_view(ids: list[str], код: int, строки: list[str]) -> tuple[int, list[str]]:
    """Свой итог из вывода общего захода: строки чужих задач убраны; код — по
    итогу своих id (дыры → 1, нечитаемая или без итога → 2), упавший заход — его код."""
    видно, коды = [], {}
    for строка in строки:
        найдено = ИТОГ_ЗАДАЧИ.match(строка)
        if найдено and найдено[1] not in ids:
            continue
        видно.append(строка)
        if найдено:
            дыр = re.match(r"дыр (\d+)", найдено[2])
            коды[найдено[1]] = (1 if int(дыр[1]) else 0) if дыр else (0 if "шаг пуст" in найдено[2] else 2)
    if код not in (0, 1, 2):
        return код, видно
    без_итога = [ид for ид in ids if ид not in коды]
    if без_итога:
        видно.append(f"мутации задач {','.join(без_итога)}: заход не дал итога (код {код})")
    return max([2 if без_итога else 0, *коды.values()]), видно


def _queued_mutations(ids: list[str]) -> int:
    """Заявка в очередь пачек: заход в срез гонит все накопленные id разом."""
    import batch_queue
    настройки = _batch_settings()
    door = str(Path(__file__).resolve().parent / "run-in-checks-slice.sh")
    команда = ["/bin/bash", door, sys.executable, str(Path(__file__).resolve()), "--мутации-задачи"]
    # Заход берёт место общего пула тяжёлых прогонов: ЦП среза — та же машина. Место
    # заявителя уже взято (его позвали через heavy-slot.sh) — второе заход не ждёт:
    # иначе N заявителей на N местах ждали бы заход, которому места нет.
    if not os.environ.get("HEAVY_SLOT_HELD"):
        команда = ["/bin/bash", str(Path(__file__).resolve().parent / "heavy-slot.sh"), "--", *команда]

    def ход(строка: str) -> None:
        # Ход захода — сразу в журнал запуска; итоги чужих задач заявителю не нужны.
        найдено = ИТОГ_ЗАДАЧИ.match(строка)
        if not (найдено and найдено[1] not in ids):
            sys.stderr.write(строка)
            sys.stderr.flush()

    код, строки = batch_queue.request(
        ids, команда, Path(конф.log_dir()) / "очередь-мутаций",
        float(настройки["окно_сбора_сек"]), int(настройки["задач"]),
        float(настройки["ждать_итога_мин"]) * 60, ход, tree=str(КОРЕНЬ))
    код, видно = _own_view(ids, код, строки)
    print("\n".join(видно))
    return код


def _record_history(ids: list[str], target: str) -> int:
    """`--история <ids> --в <файл>`: те же чтения git, что у шага «мутации», — без прогона.
    Исполнитель зовёт это вне среза и отдаёт файл заходу. Код 0 — файл записан."""
    global _recorded
    _recorded = {}
    try:
        for task in ids:
            mechanisms = механизмы_задачи(task, КОРЕНЬ)
            if mechanisms:
                mutation_paper_debts(task, КОРЕНЬ)
            for mechanism in mechanisms or []:
                task_changed_lines(task, КОРЕНЬ, mechanism)
        _git_lines(КОРЕНЬ, "rev-parse", "HEAD")
    except HistoryUnread as error:
        _event("mutations.history-unread", "error", ids=ids, причина=str(error))
        print(f"история не прочитана: {error}", flush=True)
        return 2
    Path(target).write_text(json.dumps(_recorded, ensure_ascii=False), encoding="utf-8")
    return 0


def _batch_ids() -> list[str]:
    """id пачки из довода --мутации-задачи: через запятую, без повторов."""
    довод = sys.argv[sys.argv.index("--мутации-задачи") + 1]
    return list(dict.fromkeys(и for и in довод.split(",") if и))


def _ночные_мутации() -> int:
    import yaml
    карта = yaml.safe_load(КАРТА.read_text(encoding="utf-8"))
    прибор = _mutation_probe()

    def писатель(доводы: list) -> None:
        subprocess.run([sys.executable, str(КОРЕНЬ / "scripts" / "alert-task.py"), *доводы],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=120)

    итог = ночные_мутации(карта, КОРЕНЬ, прибор, писатель)
    for ид, дыр in итог:
        print(f"  {ид}: " + ("мутаций нет" if дыр is None else f"дыр {дыр}"))
    print(f"мутации задач с долгом: {len(итог)}")
    return 0


def негодна_почему(задача: dict) -> str | None:
    """Почему проверку задачи нельзя даже запускать. `None` — можно.

    Судит ТОЛЬКО форму, без исполнения: этой же мерой отбирает задачи строитель
    (`--долг`). Прежде у него была своя — «поле проверка есть» — и 18 задач с
    негодной командой не брал никто, а перед владельцем они стояли как его
    очередь (замер 27.09.2026).
    """
    проверка = задача.get("проверка")
    if not isinstance(проверка, dict):
        return "в карточке нет поля проверка:"
    if проверка.get("след"):
        return негоден_след_почему(проверка)
    if not проверка.get("команда"):
        return "в карточке нет поля проверка: с командой"
    if not позволена(str(проверка["команда"])):
        return ("команда не из разрешённого списка: приёмка исполняет "
                "только самотесты, гейты и чтение — и никогда echo")
    if not ожидания(проверка):
        # Код возврата доказательством не является: нулём кончается почти всё,
        # включая команду, которая ничего не проверила (находка C2 ревью).
        return ("в проверке не сказано, что обязано быть в выводе "
                "(поле «ждём») — один код возврата ничего не доказывает")
    return None


# Часы жизни следа по умолчанию: сутки. Порог — довод, а не константа:
# у журнала ворот и у журнала вызовов модели ритм разный.
СЛЕД_ЧАСОВ = 24


def объявленные_следы() -> dict[str, str]:
    """Пути следов, которым приёмка верит, и КТО их пишет. Данные, не код.

    Находка ревью H1 (blocker): у команды есть `позволена` — что вообще можно
    исполнять, а у следа ограничения не было, и годился ЛЮБОЙ абсолютный путь.
    Агент, которому команда не по руке, мог написать нужную строку в /tmp
    прямо перед прогоном приёмки и закрыть задачу без единого настоящего
    прогона. Доказательство обязано принадлежать тому, кто его произвёл.
    """
    try:
        import yaml
    except ImportError:
        return {}
    try:
        данные = yaml.safe_load(_путь_форм().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}     # нечитаемо — закрыто, а не «верить любому пути»
    return {str(з["путь"]): str(з.get("пишет") or "")
            for з in (данные.get("следы") or []) if isinstance(з, dict)
            and з.get("путь") and з.get("пишет") and з.get("причина")}


def _время_записи(строка: str) -> float | None:
    """Момент СОБЫТИЯ из самой записи: поле ts в JSON-строке журнала.

    Находка ревью H2: свежесть по mtime — это «когда файл тронули», а не
    «когда случился прогон» ([[freshness-by-mtime-is-false]]). Голый `touch`
    продлевал жизнь старой записи ещё на сутки без единого нового прогона.
    """
    try:
        запись = json.loads(строка)
    except ValueError:
        return None
    метка = запись.get("ts") if isinstance(запись, dict) else None
    if isinstance(метка, str):
        return _разобрать_время(метка)
    return None


# Не всякий журнал — JSON: проба установки пишет обычную строку, начинающуюся
# датой («2026-09-28 12:13:12  === проба установки: …»). Формат следа не наш,
# и переписывать чужой журнал ради приёмки нельзя — читаем как есть.
_ВРЕМЯ_В_НАЧАЛЕ = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
    r"\s*([+-]\d{2}:?\d{2})?")


def _разобрать_время(метка: str) -> float | None:
    import datetime
    try:
        return datetime.datetime.fromisoformat(метка).timestamp()
    except ValueError:
        return None


def _время_строки(строка: str) -> float | None:
    """Время события: сперва поле ts JSON-записи, иначе дата в начале строки."""
    из_json = _время_записи(строка)
    if из_json is not None:
        return из_json
    найдено = _ВРЕМЯ_В_НАЧАЛЕ.match(строка.strip())
    if not найдено:
        return None
    дата, час, зона = найдено.groups()
    return _разобрать_время(f"{дата}T{час.replace(',', '.')}{зона or ''}")


def негоден_след_почему(проверка: dict) -> str | None:
    """Форма проверки ПО СЛЕДУ. `None` — годная.

    Зачем второй вид. Четыре карточки 28.09.2026 приёмка отвергала формой, и
    дело было НЕ в форме: их команды ей не по руке — полный прогон ворот идёт
    минутами, проба установки поднимает контейнеры, сборка публичного дерева
    делает `rsync --delete` по боевому каталогу, вызов модели стоит денег.
    Но у каждого из этих действий есть СЛЕД чужого прогона, и судить можно
    его: приёмка ничего не запускает, а читает.
    """
    след = str(проверка.get("след") or "")
    if not след.startswith("/"):
        return "след указан не абсолютным путём — искать его негде"
    объявлены = объявленные_следы()
    if след not in объявлены:
        return (f"след {след} не объявлен в acceptance-forms.yaml: приёмка "
                f"верит только журналам, у которых названы писатель и причина "
                f"— иначе доказательство пишет себе сам проверяемый")
    if проверка.get("команда"):
        return ("в проверке названы и команда, и след — что из них судить, "
                "механизму непонятно")
    if not ожидания(проверка):
        return ("в проверке не сказано, что обязано быть в следе (поле «ждём») "
                "— сам факт файла ничего не доказывает")
    часов = проверка.get("часов", СЛЕД_ЧАСОВ)
    if not isinstance(часов, int) or часов <= 0:
        return "поле «часов» должно быть целым числом больше нуля"
    return None


def судить_след(задача: dict) -> dict:
    """Вердикт по следу: файл свежий и содержит обещанное.

    Читается ПОСЛЕДНЯЯ строка: журналы дописываются, и прежние прогоны в них
    остаются навсегда — по всему файлу «красных 0» нашлось бы и через месяц
    после того, как ворота покраснели.
    """
    ид = задача.get("id", "?")
    проверка = задача["проверка"]
    путь = Path(str(проверка["след"]))
    часов = int(проверка.get("часов", СЛЕД_ЧАСОВ))
    try:
        строки = [с for с in путь.read_text(
            encoding="utf-8", errors="replace").splitlines() if с.strip()]
    except OSError as беда:
        return {"id": ид, "исход": "доказать-нечем", "след": str(путь),
                "почему": f"следа нет или он не читается: {беда}"}
    последняя = строки[-1] if строки else ""
    # Свежесть судится по времени СОБЫТИЯ из самой записи, а не по mtime
    # файла: голый touch продлевал жизнь старой записи ещё на сутки без
    # единого нового прогона (находка ревью H2).
    когда = _время_строки(последняя)
    if когда is None:
        return {"id": ид, "исход": "доказать-нечем", "след": str(путь),
                "почему": "в последней записи следа нет поля ts со временем — "
                          "по такому следу свежесть не судится"}
    возраст = (time.time() - когда) / 3600
    if возраст > часов:
        return {"id": ид, "исход": "провал", "след": str(путь),
                "почему": f"след старше {часов} ч (ему {возраст:.1f} ч) — "
                          f"прогона за это время не было"}
    нет = [ж for ж in ожидания(проверка) if ж not in последняя]
    if нет:
        return {"id": ид, "исход": "проверка-врёт", "след": str(путь),
                "почему": f"в последней записи следа нет «{нет[0]}»",
                "вывод": последняя}
    return {"id": ид, "исход": "доказано", "след": str(путь),
            "почему": f"след свежий ({возраст:.1f} ч) и содержит "
                      + " и ".join(f"«{ж}»" for ж in ожидания(проверка))}


def ожидания(проверка: dict) -> list[str]:
    """Что обязано быть в выводе. Список — потому что команд в цепочке две.

    Поле `ждём` принимает и строку, и список строк: у цепочки `A && B` ожидание
    от каждого звена своё. Пока поле было одной строкой, шесть карточек держали
    в нём фразу «X и Y», которой в выводе нет и быть не может — все шесть
    провалились на здоровых механизмах (замер 27.09.2026).
    """
    ждём = проверка.get("ждём")
    если_список = ждём if isinstance(ждём, list) else [ждём]
    return [str(ж) for ж in если_список if str(ж or "").strip()]


def _судить(задача: dict, корень: Path) -> dict:
    """Вердикт по форме и прогону. Чистая часть: её и проверяет самотест."""
    ид = задача.get("id", "?")
    проверка = задача.get("проверка") if isinstance(задача.get("проверка"), dict) else {}
    команда = str(проверка.get("команда") or "")
    беда = негодна_почему(задача)
    if беда:
        вердикт = {"id": ид, "исход": "доказать-нечем", "почему": беда}
        return {**вердикт, "команда": команда} if команда else вердикт
    if проверка.get("след"):
        return судить_след(задача)
    ждём = ожидания(проверка)
    код, вывод = прогнать(команда, корень)
    if код == -1:
        # Таймаут — тоже долг СТРОИТЕЛЯ: проверка написана такой, что не
        # укладывается в потолок. Прежде исход был «доказать-нечем», но в
        # карточку при нём не писалось ничего, а форма годная — и задача
        # выпадала из работы всех (находка ревью безопасности 27.09.2026).
        return {"id": ид, "исход": "проверка-долгая", "команда": команда,
                "почему": вывод}
    if код != 0:
        return {"id": ид, "исход": "провал", "команда": команда,
                "почему": f"команда вернула {код}", "вывод": вывод}
    нет_в_выводе = [ж for ж in ждём if ж not in вывод]
    if нет_в_выводе:
        # Команда вернула НОЛЬ, а обещанного в выводе нет: механизм работает, а
        # обещание неверно — это долг СТРОИТЕЛЯ проверок, не разработчика.
        # Замер 27.09.2026: так провалились восемь задач на здоровых механизмах,
        # потому что «ждём» писался из головы, а не с фактического вывода
        # ([[capture-foreign-output-do-not-recall-it]]). Хвост вывода едет
        # вместе с вердиктом: Bash строителю закрыт, сам он вывод не снимет.
        return {"id": ид, "исход": "проверка-врёт", "команда": команда,
                "почему": f"команда прошла (код 0), но в выводе нет «{нет_в_выводе[0]}»",
                # Вывод ПОЛНЫЙ: обрезанный хвост терял вывод первого звена
                # цепочки, а не сходилось чаще всего именно его ожидание
                # (находка G-07 ревью 27.09.2026). В карту он не попадает, а в
                # журнале потолка нет.
                "вывод": вывод}
    # Доказано — значит доказано: пометка «судит владелец» закрытие не
    # отменяет. Владелец 11.09.2026: «Я не хочу лично все проверять, в этом и
    # смысл, оставлять ему только то, чего машина проверить не может». Статус «принято»
    # говорит ровно то, что есть: доказательство машинное, и вот команда;
    # «done» по-прежнему ставится словом владельца (И-4).
    return {"id": ид, "исход": "доказано", "команда": команда,
            "почему": "вывод содержит " + " и ".join(f"«{ж}»" for ж in ждём)}


def разбор(карта: dict, корень: Path, только: set | None = None) -> list[dict]:
    активное = активное_направление()
    return [судить(з, корень)
            for з in карточки(карта, направление=активное or None)
            if з.get("status") == "test" and (только is None or з.get("id") in только)]


def свод(вердикты: list[dict]) -> dict:
    свод: dict[str, int] = {}
    for в in вердикты:
        свод[в["исход"]] = свод.get(в["исход"], 0) + 1
    return свод


def очередь_владельца(вердикты: list[dict]) -> list[dict]:
    """Задачи, которые доказать может ТОЛЬКО владелец. Это его очередь.

    Владелец 27.09.2026: «то что надо проверять мне должно быть довольно мало».
    Мало или много — видно только когда у этого есть своё число.
    """
    return [в for в in вердикты if в["исход"] == "ждёт-владельца"]


def долг_механизма(вердикты: list[dict]) -> list[dict]:
    """Задачи, которым способа проверки не хватает. Это работа строителю.

    Сюда попадает и «поля нет», и «команда есть, но форма негодная»: мерка одна
    — судья. Прежде строитель мерил своей («поле есть») и 18 задач не брал
    никогда, а они тем же числом стояли перед владельцем.
    """
    return [в for в in вердикты
            if в["исход"] in ("доказать-нечем", "проверка-врёт",
                              "проверка-долгая")]


def документ_очереди(вердикты: list[dict], задачи: list[dict]) -> str:
    """Список для владельца: что проверить и КАК. Его слово 27.09.2026 —
    «Мне нужен конкретный список с пояснениями что мне нужно проверит и как и
    это должно быть системно для пользователя».

    Пояснение берётся из карточки: `как_проверить` (шаги человеку), иначе
    `проверить_владельцу` (почему только он). Нет ни того, ни другого — так и
    сказано: это долг строителя, а не молчание.
    """
    по_ид = {з.get("id"): з for з in задачи}
    строки = ["# Что нужно проверить вам", "",
              f"Задач в вашей очереди: {len(вердикты)}. Остальное закрывает "
              "машина сама — здесь только то, чего она доказать не может.", ""]
    if not вердикты:
        строки += ["Сейчас в вашей очереди ничего нет: всё, что сделано, "
                   "доказано машиной.", ""]
    for номер, в in enumerate(вердикты, 1):
        з = по_ид.get(в["id"], {})
        строки += [f"## {номер}. {з.get('title') or в['id']}", "",
                   f"*Задача:* `{в['id']}`", ""]
        # Шаги и причина — РАЗНЫЕ вещи, и выдавать причину за шаги нельзя:
        # владелец просил «что проверить и как», а получал объяснение, чего не
        # смогла машина, под заголовком «Как проверить» (находка G-14 ревью).
        if з.get("как_проверить"):
            строки += [f"**Как проверить:** {з['как_проверить']}", ""]
        else:
            строки += ["**Шаги ещё не написаны** — это долг строителя проверок.",
                       ""]
        if з.get("проверить_владельцу"):
            строки += [f"*Машина доказать не может:* {з['проверить_владельцу']}",
                       ""]
    return "\n".join(строки)


def строки_отчёта(вердикты: list[dict]) -> list[str]:
    """По строке на КАЖДУЮ задачу, с исходом и причиной.

    Печаталось только «доказано» и «провал»: 15–20.09.2026 отчёт шесть ночей
    подряд сообщал «нечем=38», не называя ни одной задачи, и понять, чей это
    долг, было нельзя ни владельцу, ни смене.
    """
    return [f"  {в['исход']:14} {в['id']}  {в.get('почему', '')}"
            for в in вердикты]


def _проба_границ() -> int:
    """Больной случай 12.09.2026: правка уходила в соседнюю карточку.

    Поля карточки проходились классом «любой пробел», а он включает перевод
    строки: у задачи с пустой строкой внутри блока приёмка могла поставить
    статус соседу. Здесь это проверяется на карте с ДВУМЯ задачами.
    """
    import tempfile
    карта = (
        "tasks:\n"
        "      - id: своя\n"
        "        status: test\n"
        "\n"
        "      - id: соседняя\n"
        "        status: test\n")
    неудач = 0
    with tempfile.TemporaryDirectory() as врем:
        файл = Path(врем) / "dev-map.yaml"
        файл.write_text(карта, encoding="utf-8")
        закрыто, _ = применить([{"id": "своя", "исход": "доказано"}], файл)
        стало = файл.read_text(encoding="utf-8")
        своя = разбор_карточки.блок(стало, "своя")[0]
        соседняя = разбор_карточки.блок(стало, "соседняя")[0]
        if закрыто != 1 or "status: принято" not in своя:
            print("  ПЛОХО своя карточка не принята"); неудач += 1
        else:
            print("  ок    своя карточка принята")
        if "status: test" not in соседняя or "принято" in соседняя:
            print("  ПЛОХО правка ушла в СОСЕДНЮЮ карточку"); неудач += 1
        else:
            print("  ок    соседняя карточка не тронута")
    return неудач


def _selftest() -> int:
    import tempfile
    плохо = 0
    путей = [0]

    def проба(имя: str, ждём, вышло) -> None:
        nonlocal плохо
        путей[0] += 1
        if вышло == ждём:
            print(f"  ок    {имя}")
        else:
            print(f"  ПЛОХО {имя}: ждали {ждём!r}, вышло {вышло!r}")
            плохо = 1

    with tempfile.TemporaryDirectory() as врем:
        корень = Path(врем)
        # БОЛЬНОЙ СЛУЧАЙ: задача без поля проверки не закрывается молча.
        проба("БОЛЬНОЙ СЛУЧАЙ: нет поля проверка: — доказать нечем",
              "доказать-нечем",
              судить({"id": "a", "status": "test"}, корень)["исход"])
        # ── БОЛЬНОЙ СЛУЧАЙ 27.09.2026: 18 задач залегли между двумя мерками ──
        # Владелец: «куча всего лежит в статусе тест а я даже об этом не знаю».
        # Замер того дня: 29 задач в `test`, у 18 проверка БЫЛА НАПИСАНА и
        # отвергалась по форме — чаще всего цепочка «самотест && самотест».
        # Строитель такую задачу больше не берёт (поле-то есть), судья не
        # принимает — и она стоит в очереди владельца навсегда.
        проба("БОЛЬНОЙ СЛУЧАЙ: цепочка из разрешённых звеньев — разрешена",
              True,
              позволена("python3 scripts/acceptance.py --selftest && "
                        "bash harness/demons/ton-watch.sh --selftest"))
        проба("цепочка с запрещённым звеном — запрещена целиком", False,
              позволена("python3 scripts/acceptance.py --selftest && "
                        "bash scripts/publish.sh"))
        проба("одиночный «&» (запуск в фон) — запрещён", False,
              позволена("python3 scripts/acceptance.py --selftest &"))
        # Скрипты, которые ПО ИМЕНИ являются тестами, флага не требуют: у
        # test_*.sh самотеста нет, он и есть проба целиком.
        # БОЛЬНОЙ СЛУЧАЙ 27.09.2026 (находка ревью безопасности, critical):
        # разрешив цепочку, форма «python3 scripts/<имя>.py без флага» впустила
        # `build-public.py` — а он делает rsync --delete по боевому каталогу
        # публичной сборки. Живой прогон в 21:04 это и выполнил. Форма без флага
        # годится только для гейтов и проб, а они в проекте зовутся check-* и
        # test_*; всё прочее обязано называть --selftest.
        проба("БОЛЬНОЙ СЛУЧАЙ: сборка публичного пакета — запрещена", False,
              позволена("python3 scripts/build-public.py"))
        проба("и в цепочке тоже запрещена", False,
              позволена("python3 scripts/build-public.py && "
                        "python3 scripts/check-public-clean.py"))
        проба("гейт по имени check-* без флага — разрешён", True,
              позволена("python3 scripts/check-public-clean.py"))
        проба("проба по имени test_* без флага — разрешена", True,
              позволена("python3 scripts/test_version.py"))
        проба("любой скрипт с --selftest — разрешён", True,
              позволена("python3 scripts/version.py --selftest"))
        проба("скрипт не гейт и без флага — запрещён", False,
              позволена("python3 scripts/version.py"))
        # БОЛЬНОЙ СЛУЧАЙ 15.10.2026: общий запрет ловил слово «deploy» и
        # «docker» в ИМЕНИ законной пробы — отвергнуты были ВСЕ ШЕСТЬ проб ворот
        # выката и установки docker. Значит любая задача про выкат или про
        # установку не могла быть закрыта машиной и висела в очереди владельца
        # вечно. Запрет обязан судить ДОВОДЫ команды, а не имя её скрипта.
        проба("БОЛЬНОЙ СЛУЧАЙ: проба ворот выката — «deploy» в ИМЕНИ, разрешена",
              True, позволена("python3 scripts/test_deploy_guard.py"))
        проба("проба установки docker — «docker» в ИМЕНИ, разрешена",
              True, позволена("bash scripts/test_install_docker.sh"))
        проба("а сам выкат по-прежнему запрещён", False,
              позволена("bash scripts/deploy.sh"))
        # Форма выбрана так, чтобы доводы ПРИНИМАЛИСЬ образцом: у
        # «python3 scripts/test_…py --docker» лишний довод отвергает сам
        # fullmatch, и случай был бы зелен по второй причине, не проверив
        # запрет действий вовсе (находка ревью 15.10.2026).
        проба("запрещённое слово в доводах bash-пробы запрещено", False,
              позволена("bash scripts/test_x.sh --docker"))
        проба("и слово выката в доводах bash-пробы запрещено", False,
              позволена("bash scripts/test_x.sh --deploy"))
        проба("а законный ключ той же формы разрешён", True,
              позволена("bash scripts/test_x.sh --быстро"))
        проба("выход за корень в пути запрещён и у пробы", False,
              позволена("python3 scripts/../test_version.py"))
        проба("тестовый скрипт по имени — разрешён", True,
              позволена("bash scripts/test_memory_commit.sh"))
        проба("не тестовый скрипт без флага — запрещён", False,
              позволена("bash scripts/tg_send.sh"))
        # ── Очередь владельца ≠ долг механизма (находка C5 ревью 11.09.2026,
        # закрытая наполовину: строитель такие задачи не берёт, а приёмка их
        # по-прежнему считала вместе с неразобранными) ──
        проба("БОЛЬНОЙ СЛУЧАЙ: пометка «только владелец» — свой исход",
              "ждёт-владельца",
              судить({"id": "i", "status": "test",
                      "проверить_владельцу": "вид на экране"}, корень)["исход"])
        # Пометка НЕ отменяет машинного доказательства: есть годная проверка —
        # закрываем без владельца (его слово 11.09: «надо только то, что не
        # может проверить система»).
        проба("пометка не отменяет годную проверку", "доказано",
              судить({"id": "j", "status": "test",
                      "проверить_владельцу": "вид",
                      "проверка": {"команда": "grep -c . /etc/hostname",
                                   "ждём": "1"}}, корень)["исход"])
        # ── БОЛЬНОЙ СЛУЧАЙ 27.09.2026, вторая волна: цепочка заработала, и
        # восемь задач стали «провалом». Прогон каждой команды руками: все
        # возвращают 0. Не сходилось поле «ждём» — в шести карточках стояла
        # СОСТАВНАЯ фраза «X и Y» на цепочку из двух команд (такой строки в
        # выводе нет и быть не может), в трёх — слово, которого гейт не печатает.
        проба("«ждём» списком: все строки в выводе — доказано", "доказано",
              судить({"id": "l", "status": "test",
                      "проверка": {"команда": "grep -c . /etc/hostname",
                                   "ждём": ["1"]}}, корень)["исход"])
        # Обещание неверно, а механизм работает (код 0) — это долг СТРОИТЕЛЯ
        # проверок, не разработчика: чинится текст карточки, а не код.
        вердикт_врёт = судить({"id": "m", "status": "test",
                               "проверка": {"команда": "grep -c . /etc/hostname",
                                            "ждём": ["1", "такой строки нет"]}},
                              корень)
        проба("БОЛЬНОЙ СЛУЧАЙ: код 0, а строки нет — врёт проверка, не код",
              "проверка-врёт", вердикт_врёт["исход"])
        проба("названа именно та строка, которой не хватило", True,
              "такой строки нет" in вердикт_врёт["почему"])
        # А вот ненулевой код — сломан механизм, и это работа разработчика.
        проба("ненулевой код остаётся провалом", "провал",
              судить({"id": "n", "status": "test",
                      "проверка": {"команда": "grep -c нетакого /etc/hostname",
                                   "ждём": ["1"]}}, корень)["исход"])
        проба("пустой список «ждём» ничего не доказывает", "доказать-нечем",
              судить({"id": "o", "status": "test",
                      "проверка": {"команда": "grep -c . /etc/hostname",
                                   "ждём": []}}, корень)["исход"])
        # Команда прошла, ожидаемое найдено, задача внутренняя — доказано.
        проба("команда прошла и вывод ожидаемый — доказано", "доказано",
              судить({"id": "b", "status": "test", "owner_testable": False,
                      "проверка": {"команда": "grep -c . /etc/hostname",
                                   "ждём": "1"}}, корень)["исход"])
        # Пометка «судит владелец» доказанность не отменяет: владелец просил
        # не проверять руками то, что машина уже доказала.
        проба("пометка «судит владелец» закрытие не отменяет", "доказано",
              судить({"id": "c", "status": "test", "owner_testable": True,
                      "проверка": {"команда": "grep -c . /etc/hostname",
                                   "ждём": "1"}}, корень)["исход"])
        # Команда прошла, но ожидаемого нет — это провал, а не успех.
        проба("вывод без ожидаемого — провал", "провал",
              судить({"id": "d", "status": "test", "owner_testable": False,
                      "проверка": {"команда": "grep -c root /etc/hostname",
                                   "ждём": "ЗЕЛЁНЫЙ"}}, корень)["исход"])
        # БОЛЬНОЙ СЛУЧАЙ ревью 11.09.2026: доказательство подделывалось одной
        # строкой — «echo ДОКАЗАНО» при «ждём: ДОКАЗАНО» давало «доказано».
        проба("БОЛЬНОЙ СЛУЧАЙ: echo не доказательство", "доказать-нечем",
              судить({"id": "f", "status": "test", "owner_testable": False,
                      "проверка": {"команда": "echo ДОКАЗАНО", "ждём": "ДОКАЗАНО"}},
                     корень)["исход"])
        # И второй оттуда же: карточки пишет НОЧНОЙ АГЕНТ, а команда шла в
        # оболочку без ограничителей — git push прошёл бы мимо И-2.
        проба("БОЛЬНОЙ СЛУЧАЙ: опасная команда не исполняется", "доказать-нечем",
              судить({"id": "g", "status": "test", "owner_testable": False,
                      "проверка": {"команда": "git push origin master",
                                   "ждём": "ок"}},
                     корень)["исход"])
        # БОЛЬНОЙ СЛУЧАЙ ревью: перевод строки внутри «безопасного» пути
        # проносил вторую команду, и оболочка исполняла обе.
        проба("перевод строки в команде — запрещён", False,
              позволена("test -s /etc/hostname\ntouch улика"))
        # БОЛЬНОЙ СЛУЧАЙ ревью: образец `bash scripts/*.sh` разрешал скрипт
        # выката — то есть выкат в прод из ночного cron мимо И-2.
        проба("скрипт выката — запрещён", False,
              позволена("bash scripts/deploy.sh"))
        проба("скрипт публикации — запрещён", False,
              позволена("bash scripts/publish.sh"))
        # БОЛЬНОЙ СЛУЧАЙ ревью: проверка без «ждём» доказывалась кодом возврата.
        проба("проверка без «ждём» ничего не доказывает", "доказать-нечем",
              судить({"id": "h", "status": "test", "owner_testable": False,
                      "проверка": {"команда": "python3 scripts/archive-map.py"}},
                     корень)["исход"])
        # Находка G-18 ревью 27.09.2026: `ФОРМЫ_СЛОВАМИ` (их читает промпт
        # строителя) и `РАЗРЕШЁННЫЕ` — два писателя на один факт, и они уже
        # разъехались. 12.09.2026 на таком же расхождении пропали все 19
        # написанных проверок.
        образцы_по_словам = {
        "python3 scripts/<имя>.py --selftest": "python3 scripts/acceptance.py --selftest",
        "python3 scripts/check-<имя>.py               (гейт: без флага можно)":
            "python3 scripts/check-secrets.py",
        "python3 scripts/test_<имя>.py                (проба: без флага можно)":
            "python3 scripts/test_version.py",
        "bash scripts/<имя>.sh --selftest": "bash scripts/tg_send.sh --selftest",
        "bash harness/demons/<имя>.sh --selftest":
            "bash harness/demons/ton-watch.sh --selftest",
        "python3 harness/panel/test_<имя>.py": "python3 harness/panel/test_vhod.py",
        "test -s <путь относительно корня проекта>": "test -s dev-map.yaml",
        "grep -c '<образец>' <путь относительно корня проекта>":
            "grep -c 'ок' dev-map.yaml",
        "curl -s http://127.0.0.1<адрес>": "curl -s http://127.0.0.1:8080/health",
        "bash scripts/test_<имя>.sh              (скрипт-проба, флаг не нужен)":
            "bash scripts/test_memory_commit.sh",
        "python3 scripts/alert-task.py --is-green <ключ тревоги>   (карточка тревоги сторожа)":
            "python3 scripts/alert-task.py --is-green nightly-gates:red",
        "любые две-три формы выше через «&&»     (ждём тогда СПИСКОМ, по строке на звено)":
            "python3 scripts/acceptance.py --selftest && test -s dev-map.yaml",
        }
        проба("у каждой формы словами есть образец в пробе", set(ФОРМЫ_СЛОВАМИ),
          set(образцы_по_словам))
        не_прошли = [слова for слова, образец in образцы_по_словам.items()
                 if not позволена(образец)]
        проба("БОЛЬНОЙ СЛУЧАЙ: каждая форма из промпта строителя проходит гейт",
          [], не_прошли)

        # ── поимённые разрешения: данные с причиной, а не шаблон ──────────────
        # Замер 28.09.2026: из 34 карточек с полем «проверка» механизм отвергал
        # 14 формой, и задача висела в очереди ВЛАДЕЛЬЦА вместо закрытия
        # машиной. Список — данные: точная строка плюс причина.
        прежний_путь = os.environ.get("ACCEPTANCE_FORMS")
        try:
            стенд_форм = Path(tempfile.mkdtemp()) / "формы.yaml"
            стенд_форм.write_text(
                "по_имени:\n"
                "  - команда: \"python3 scripts/tool.py --читающий\"\n"
                "    причина: «только читает»\n"
                "  - команда: \"python3 scripts/tool.py --без-причины\"\n",
                encoding="utf-8")
            os.environ["ACCEPTANCE_FORMS"] = str(стенд_форм)
            проба("названная в данных команда разрешена",
                  True, позволена("python3 scripts/tool.py --читающий"))
            проба("БОЛЬНОЙ СЛУЧАЙ: запись без причины команду НЕ разрешает",
                  False, позволена("python3 scripts/tool.py --без-причины"))
            проба("неназванная команда отвергнута по-прежнему",
                  False, позволена("python3 scripts/tool.py --чужой"))
            проба("названная команда годится и звеном цепочки", True,
                  позволена("python3 scripts/check-pipeline.py --selftest && "
                            "python3 scripts/tool.py --читающий"))
            os.environ["ACCEPTANCE_FORMS"] = str(стенд_форм.parent / "нет-такого.yaml")
            проба("БОЛЬНОЙ СЛУЧАЙ: файла данных нет — поимённых разрешений нет",
                  False, позволена("python3 scripts/tool.py --читающий"))
            проба("опасное шаблоном не проходит и с данными", False,
                  позволена("python3 scripts/build-public.py"))
        finally:
            if прежний_путь is None:
                os.environ.pop("ACCEPTANCE_FORMS", None)
            else:
                os.environ["ACCEPTANCE_FORMS"] = прежний_путь
        проба("боевой список форм не пуст и у каждой записи есть причина",
              True, len(поимённо()) >= 1)
        # БОЛЬНОЙ СЛУЧАЙ (S-08 ревью безопасности 27.09.2026): класс пути
        # пропускал «..», и агент-строитель мог позвать файл вне дерева.
        проба("БОЛЬНОЙ СЛУЧАЙ: выход из дерева через «..» — запрещён", False,
              позволена("python3 scripts/../../../tmp/evil.py"))
        проба("и у тестового скрипта тоже", False,
              позволена("bash scripts/test_../../x.sh"))
        проба("самотест гейта — разрешён", True,
              позволена("python3 scripts/acceptance.py --selftest"))
        проба("самотест демона — разрешён", True,
              позволена("bash harness/demons/ton-watch.sh --selftest"))
        # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: промпт строителя называл формы, которых
        # гейт не принимает, — все 19 написанных проверок были запрещёнными.
        проба("grep с образцом в кавычках — разрешён", True,
              позволена("grep -c 'def test_' app/tests/api/test_x.py"))
        проба("прогон тестов — запрещён", False,
              позволена("cd app && python3 -m pytest tests/ -v"))
        проба("абсолютный путь вне проекта — запрещён", False,
              позволена("test -s /etc/shadow; cat /etc/shadow"))
        проба("команда с подстановкой — запрещена", False,
              позволена("test -s $(echo /etc/passwd)"))

        # Ненулевой код — провал, даже если ожидаемое слово в выводе есть.
        проба("ненулевой код — провал", "провал",
              судить({"id": "e", "status": "test", "owner_testable": False,
                      "проверка": {"команда": "grep -c нетакого /etc/hostname",
                                   "ждём": "1"}}, корень)["исход"])
    # Направление фильтрует: задача чужого направления в разбор не попадает.
    дерево = {"lines": [
        {"id": "harness", "layer": "infra", "tasks": [
            {"id": "hr.своя", "status": "test"}]},
        {"id": "app", "layer": "product", "tasks": [
            {"id": "pr.чужая", "status": "test"}]}]}
    проба("все направления — обе задачи", 2, len(карточки(дерево)))
    проба("активное направление — только своя", ["hr.своя"],
          [з["id"] for з in карточки(дерево, направление="harness")])
    проба("направление продукта — только его", ["pr.чужая"],
          [з["id"] for з in карточки(дерево, направление="app")])

    # ── БОЛЬНОЙ СЛУЧАЙ 27.09.2026: долг был невидим ──
    # Печать называла только «доказано» и «провал». 15–20.09 приёмка шесть
    # ночей печатала «нечем=38» без единой строки о том, ЧТО это за задачи, а
    # демон при «принято=0 провал=0» вообще молчал в канал. Владелец: «куча
    # всего лежит в статусе тест а я даже об этом не знаю и не понимаю».
    вердикты_пробы = [
        {"id": "т.доказана", "исход": "доказано", "почему": "вывод содержит «ок»"},
        {"id": "т.нечем", "исход": "доказать-нечем", "почему": "форма не та"},
        {"id": "т.владельцу", "исход": "ждёт-владельца", "почему": "вид на экране"},
    ]
    проба("отчёт называет КАЖДУЮ задачу, а не только доказанные", 3,
          len(строки_отчёта(вердикты_пробы)))
    # БОЛЬНОЙ СЛУЧАЙ 27.09.2026: владелец просил «конкретный список с
    # пояснениями что мне нужно проверит и как». Числа мало: «твоя очередь 7»
    # не говорит, что открыть и что увидеть.
    док = документ_очереди(
        [в for в in вердикты_пробы if в["исход"] == "ждёт-владельца"],
        [{"id": "т.владельцу", "title": "Кнопка паузы",
          "как_проверить": "нажми «Пауза» в меню и убедись, что смена встала"}])
    проба("БОЛЬНОЙ СЛУЧАЙ: список говорит, ЧТО открыть и что увидеть", True,
          "нажми «Пауза»" in док and "Кнопка паузы" in док)
    проба("в списке ровно задачи очереди владельца", 1, док.count("## "))
    без_пояснения = документ_очереди(
        [{"id": "т.нема", "исход": "ждёт-владельца", "почему": "x"}],
        [{"id": "т.нема", "title": "Без пояснения"}])
    проба("нет шагов — долг назван, а не выдан за инструкцию", True,
          "Шаги ещё не написаны" in без_пояснения)
    проба("очередь владельца — своё число", 1,
          len(очередь_владельца(вердикты_пробы)))
    проба("долг механизма — своё число", 1,
          len(долг_механизма(вердикты_пробы)))

    # БОЛЬНОЙ СЛУЧАЙ ревью кода 11.09.2026 (C6): `применить()` правит ЖИВУЮ
    # карту регулярным выражением и не был покрыт ни одной пробой — мутант со
    # сломанной регуляркой давал 10 зелёных из 10. Здесь проверяется, что
    # правка меняет РОВНО две вещи и не трогает соседей.
    import tempfile
    образец = """version: 1
lines:
  - id: harness
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.доказана
        status: test
        owner_testable: true
        title: "Доказана"
        summary: "текст"
      - id: t.соседка
        status: test
        owner_testable: true
        title: "Соседка"
        summary: "её трогать нельзя"
queue: []
"""
    with tempfile.TemporaryDirectory() as врем:
        карта = Path(врем) / "dev-map.yaml"
        карта.write_text(образец, encoding="utf-8")
        закрыто, вернулось = применить(
            [{"id": "t.доказана", "исход": "доказано"}], карта)
        стало = карта.read_text(encoding="utf-8")
        проба("применить: закрыта одна задача", (1, 0), (закрыто, вернулось))
        проба("применить: статус сменился на «принято»", True,
              "status: принято" in стало)
        проба("применить: дата приёмки проставлена", True,
              "принято_когда:" in стало)
        проба("БОЛЬНОЙ СЛУЧАЙ: соседняя задача не тронута", True,
              "- id: t.соседка\n        status: test" in стало)
        проба("применить: секции после задач на месте", True,
              "queue: []" in стало)
        import yaml as _yaml
        проба("применить: карта осталась валидным YAML", True,
              isinstance(_yaml.safe_load(стало), dict))
        # БОЛЬНОЙ СЛУЧАЙ ревью (I3): повторное принятие давало ВТОРОЙ ключ
        # принято_когда, и YAML брал прежнюю, неверную дату.
        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "доказано"}], карта)
        применить([{"id": "t.доказана", "исход": "доказано"}], карта)
        проба("повторное принятие не плодит дубль даты", 1,
              карта.read_text(encoding="utf-8").count("принято_когда:"))

        # БОЛЬНОЙ СЛУЧАЙ 27.09.2026: «команда прошла, а обещанного нет» лежало
        # в одной куче с настоящим провалом, и восемь задач на ЗДОРОВЫХ
        # механизмах ждали разработчика вместо строителя. Пометка несёт хвост
        # вывода: Bash строителю закрыт, сам он вывод не снимет.
        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "проверка-врёт",
                    "почему": "команда прошла (код 0), но в выводе нет «оба зелёные»",
                    "вывод": "SELFTEST: зелёный (14 путей)"}], карта)
        стало_врёт = карта.read_text(encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: вранье проверки помечено в карточке", True,
              "проверка_врёт:" in стало_врёт)
        # Хвост вывода лежит в журнале, а карточка называет путь к нему:
        # ревью безопасности 27.09.2026 показало, что дословный вывод в карте —
        # утечка в git (И-3).
        проба("карточка называет путь к сохранённому выводу", True,
              "priyomka/t.доказана.вывод" in стало_врёт)
        проба("сам вывод лежит в журнале, а не в карте", True,
              "SELFTEST: зелёный" in
              (Path(конф.log_dir()) / "priyomka" / "t.доказана.вывод")
              .read_text(encoding="utf-8"))
        проба("статус при вранье проверки не меняется", True,
              "- id: t.доказана\n        status: test" in стало_врёт)
        # БОЛЬНОЙ СЛУЧАЙ 27.09.2026: строитель, поставив «проверить_владельцу»,
        # больше эту задачу не берёт — значит инструкцию «как проверить» для
        # человека не напишет НИКОГДА, и список владельцу выходит с
        # объяснением, почему машина не смогла, вместо шагов, что ему открыть.
        проба("БОЛЬНОЙ СЛУЧАЙ: пометка без инструкции — долг строителя", True,
              bool(нет_инструкции({"проверить_владельцу": "вид на экране"})))
        проба("пометка с инструкцией строителю не возвращается", None,
              нет_инструкции({"проверить_владельцу": "вид",
                              "как_проверить": "открой панель и посмотри"}))
        проба("врущая задача попадает в работу строителя", True,
              bool(пометка_долга({"проверка_врёт": "27.09: нет строки"})))
        применить([{"id": "t.доказана", "исход": "доказано"}], карта)
        проба("доказанная задача теряет и пометку вранья", False,
              "проверка_врёт:" in карта.read_text(encoding="utf-8"))

        # БОЛЬНОЙ СЛУЧАЙ (находка ревью безопасности 27.09.2026, critical):
        # текст вердикта подставлялся в ШАБЛОН ЗАМЕНЫ регулярного выражения.
        # Обратный слэш в выводе команды (git печатает кириллицу как \320\277)
        # ронял `применить` целиком: карта не записывалась вовсе, принятые
        # задачи оставались в test, а демон считал прогон здоровым.
        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "проверка-врёт",
                    "почему": r"нет строки «\q»",
                    "вывод": r"grep: неверный шаблон \q; путь \320\277"}], карта)
        с_слэшем = карта.read_text(encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: обратный слэш в выводе не роняет запись карты",
              True, "проверка_врёт:" in с_слэшем)
        проба("и не подставляет в поле текст самой карточки", False,
              "status: test строке" in с_слэшем)
        # БОЛЬНОЙ СЛУЧАЙ (G-06 ревью 27.09.2026): строитель поправил «ждём»,
        # снова не сошлось — а в карточке навсегда оставалась ПЕРВАЯ причина, и
        # он правил по обещанию, которого в карточке уже нет.
        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "проверка-врёт",
                    "почему": "нет «первое»"}], карта)
        применить([{"id": "t.доказана", "исход": "проверка-врёт",
                    "почему": "нет «второе»"}], карта)
        дважды = карта.read_text(encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: пометка обновилась на новую причину", True,
              "нет «второе»" in дважды)
        проба("и прежняя причина не осталась", False, "нет «первое»" in дважды)
        проба("дубля ключа не появилось", 1, дважды.count("проверка_врёт:"))

        # БОЛЬНОЙ СЛУЧАЙ (находка ревью безопасности 27.09.2026, critical, И-3):
        # хвост вывода писался в карту ДОСЛОВНО, а карта лежит в git. Вывод
        # команды несёт что угодно, включая DSN с паролем — ровно то, что уже
        # утекало в лог прогона ([[make-печатает-пароль-в-лог]]).
        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "проверка-врёт",
                    "почему": "нет строки «ок»",
                    "вывод": "DSN=postgresql://app:S3cretPass@127.0.0.1/app"}],  # не-секрет
                  карта)
        с_секретом = карта.read_text(encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: вывод команды в карту не попадает", False,
              "S3cretPass" in с_секретом)
        проба("но карточка говорит, ГДЕ лежит вывод", True,
              "вывод:" in с_секретом.split("проверка_врёт:")[1].split("\n")[0])

        # БОЛЬНОЙ СЛУЧАЙ (ревью безопасности 27.09.2026): не уложившаяся в
        # потолок проверка получала исход, при котором в карточку не писалось
        # НИЧЕГО, а форма у неё годная — задача выпадала из работы всех сразу.
        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "проверка-долгая",
                    "почему": "проверка не уложилась в 240 с"}], карта)
        долгая = карта.read_text(encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: долгая проверка помечена в карточке", True,
              "проверка_долгая:" in долгая)
        проба("и возвращается строителю", True,
              bool(пометка_долга({"проверка_долгая": "27.09: не уложилась"})))
        применить([{"id": "t.доказана", "исход": "доказано"}], карта)
        проба("доказанная задача теряет и пометку долгой проверки", False,
              "проверка_долгая:" in карта.read_text(encoding="utf-8"))

        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "провал",
                    "почему": r"команда вернула 1: \1 \g<1>"}], карта)
        проба("слэш в причине провала тоже безопасен", True,
              "провал_проверки:" in карта.read_text(encoding="utf-8"))

        # Провал ВИДЕН в карточке: иначе разработчик узнает о нём только из
        # отчёта за ту ночь, а отчёты никто не перечитывает (находка I2).
        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "провал",
                    "почему": "в выводе нет ожидаемого"}], карта)
        проба("провал записан в карточку", True,
              "провал_проверки:" in карта.read_text(encoding="utf-8"))
        применить([{"id": "t.доказана", "исход": "провал",
                    "почему": "снова"}], карта)
        проба("повторный провал не плодит дубль ключа", 1,
              карта.read_text(encoding="utf-8").count("провал_проверки:"))

        # БОЛЬНЫЙ СЛУЧАЙ 12.09.2026: задача провалила проверку, была починена
        # и доказана — а пометка провала осталась в карточке. Прибор очереди
        # ставит провал вторым по важности признаком, и доказанная задача
        # висела бы первой строкой «что делать сейчас» вечно.
        применить([{"id": "t.доказана", "исход": "доказано"}], карта)
        после = карта.read_text(encoding="utf-8")
        проба("БОЛЬНОЙ СЛУЧАЙ: доказанная задача теряет пометку провала",
              False, "провал_проверки:" in после)
        проба("и при этом закрывается", True, "status: принято" in после)

        карта.write_text(образец, encoding="utf-8")
        # Провал статус НЕ меняет: чинит разработчик, а не проверяющий.
        карта.write_text(образец, encoding="utf-8")
        применить([{"id": "t.доказана", "исход": "провал"}], карта)
        проба("провал статус не меняет", True,
              "- id: t.доказана\n        status: test" in
              карта.read_text(encoding="utf-8"))

    # ── ОТБОР СТРОИТЕЛЯ: путь, на который пересажен весь его прогон ──
    # Находка G-11 ревью 27.09.2026: этот путь не покрывала ни одна проба, и
    # мутации «убрать пометку долга из отбора» и «вернуть пустой список»
    # оставались зелёными.
    стенд = {"lines": [{"id": "harness", "layer": "infra", "tasks": [
        {"id": "т.нет-проверки", "status": "test", "title": "Нет"},
        {"id": "т.годная", "status": "test", "title": "Годная",
         "проверка": {"команда": "python3 scripts/check-secrets.py",
                      "ждём": "чисто"}},
        {"id": "т.негодная", "status": "test", "title": "Негодная",
         "проверка": {"команда": "echo ок", "ждём": "ок"}},
        {"id": "т.врёт", "status": "test", "title": "Врёт",
         "проверка": {"команда": "python3 scripts/check-secrets.py",
                      "ждём": "чисто"},
         "проверка_врёт": "27.09: нет строки «чисто»"},
        {"id": "т.владельцу-без-шагов", "status": "test", "title": "Ему",
         "проверить_владельцу": "вид на экране"},
        {"id": "т.владельцу-с-шагами", "status": "test", "title": "Ему же",
         "проверить_владельцу": "вид", "как_проверить": "открой панель"},
        {"id": "т.владельцу-и-врёт", "status": "test", "title": "И то и то",
         "проверить_владельцу": "вид", "как_проверить": "открой панель",
         "проверка_врёт": "27.09: нет строки"},
        {"id": "т.не-в-тесте", "status": "plan", "title": "Рано"},
    ]}]}
    взято = {ид for ид, _, _ in долг_строителя(стенд, направление="harness")}
    проба("отбор: задача без проверки — берётся", True,
          "т.нет-проверки" in взято)
    проба("отбор: негодная форма — берётся", True, "т.негодная" in взято)
    проба("БОЛЬНОЙ СЛУЧАЙ: врущая проверка — берётся", True,
          "т.врёт" in взято)
    проба("БОЛЬНОЙ СЛУЧАЙ (G-04): помеченная владельцу с врущей проверкой — берётся",
          True, "т.владельцу-и-врёт" in взято)
    проба("отбор: пометка без шагов — берётся", True,
          "т.владельцу-без-шагов" in взято)
    проба("отбор: годная проверка — НЕ берётся", False, "т.годная" in взято)
    проба("отбор: пометка с шагами и без вранья — НЕ берётся", False,
          "т.владельцу-с-шагами" in взято)
    проба("отбор: задача не в test — НЕ берётся", False,
          "т.не-в-тесте" in взято)

    # БОЛЬНОЙ СЛУЧАЙ (ревью безопасности 27.09.2026): таймаут убивал оболочку,
    # а работающее звено оставалось сиротой и продолжало править файлы.
    # Потолок проб — секунды, а не боевые 120: поведение то же, а самотест не
    # спит шесть минут. Замер 28.09.2026: 367 с сна на прогон, и полные ворота
    # зовут этот самотест дважды.
    ПОТОЛОК_ПРОБЫ = 3
    with tempfile.TemporaryDirectory() as врем:
        улика = Path(врем) / "сирота-дожила"
        код, почему = прогнать(
            f"python3 -c \"import time,pathlib;time.sleep({ПОТОЛОК_ПРОБЫ + 4});"
            f"pathlib.Path(r'{улика}').write_text('жива')\"", Path(врем),
            ПОТОЛОК_ПРОБЫ)
        проба("таймаут назван отдельным кодом", -1, код)
        проба("БОЛЬНОЙ СЛУЧАЙ: после таймаута сирота не дожила", False,
              улика.exists())
    проба("потолок растёт с числом звеньев", True,
          f"{ПОТОЛОК_ПРОБЫ * 2} с" in прогнать(
              f"python3 -c \"import time;time.sleep({ПОТОЛОК_ПРОБЫ * 2 + 3})\" && true",
              Path.cwd(), ПОТОЛОК_ПРОБЫ)[1])
    # Боевое умолчание обязано остаться боевым: проба с потолком не должна
    # менять то, чем судят задачи.
    проба("боевой потолок остался 120 с", 120, ПОТОЛОК_СЕК)

    # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: итог печатается в stdout, а следом идёт
    # длинный stderr. Обрезка вывода ДО сравнения выкидывала итоговую строку,
    # и исправный механизм получал «провалила проверку» — задача поднималась
    # на вершину очереди как сломанная. Условие создаётся ровно такое: итог
    # первым, шум после, шума больше прежнего потолка обрезки.
    шумная = ("python3 -c \"import sys; print('неудач: 0'); "
              "sys.stderr.write('ш'*5000)\"")
    _, вывод_шум = прогнать(шумная, Path.cwd())
    проба("БОЛЬНОЙ СЛУЧАЙ: итог в stdout не теряется за длинным stderr",
          True, "неудач: 0" in вывод_шум)

    # ── проверка по СЛЕДУ чужого прогона (четыре карточки 28.09.2026) ──
    import tempfile
    with tempfile.TemporaryDirectory(prefix="след-") as врем:
        стенд = Path(врем)
        журнал = стенд / "ворота-итог.jsonl"
        формы = стенд / "формы.yaml"
        формы.write_text(
            "следы:\n"
            f"  - путь: \"{журнал}\"\n"
            "    пишет: \"стенд пробы\"\n"
            "    причина: \"проба судейства по следу\"\n", encoding="utf-8")
        прежние_формы = os.environ.get("ACCEPTANCE_FORMS")
        os.environ["ACCEPTANCE_FORMS"] = str(формы)
        try:
            сейчас = time.strftime("%Y-%m-%dT%H:%M:%S")
            давно = time.strftime("%Y-%m-%dT%H:%M:%S",
                                  time.localtime(time.time() - 80 * 3600))
            журнал.write_text(
                f'{{"ts":"{давно}","режим":"всё","зелёных":160,"красных":3}}\n'
                f'{{"ts":"{сейчас}","режим":"всё","зелёных":164,"красных":0}}\n',
                encoding="utf-8")
            карточка = {"id": "hr.проба", "проверка": {
                "след": str(журнал), "ждём": ['"режим":"всё"', '"красных":0']}}
            проба("след свежий и с обещанным — доказано",
                  "доказано", судить_след(карточка)["исход"])

            # БОЛЬНОЙ СЛУЧАЙ: прежний прогон зелёный, последний красный —
            # судим ПОСЛЕДНЮЮ запись, иначе «красных 0» нашлось бы в любом файле.
            журнал.write_text(
                f'{{"ts":"{давно}","режим":"всё","зелёных":164,"красных":0}}\n'
                f'{{"ts":"{сейчас}","режим":"всё","зелёных":160,"красных":3}}\n',
                encoding="utf-8")
            проба("БОЛЬНОЙ СЛУЧАЙ: красный последний прогон не закрыть старой записью",
                  "проверка-врёт", судить_след(карточка)["исход"])

            # БОЛЬНОЙ СЛУЧАЙ H2: файл ТРОНУТ, а прогона не было. Прежде
            # свежесть считалась по mtime, и голый touch продлевал старую
            # запись ещё на сутки ([[freshness-by-mtime-is-false]]).
            журнал.write_text(
                f'{{"ts":"{давно}","режим":"всё","зелёных":164,"красных":0}}\n',
                encoding="utf-8")
            os.utime(журнал, None)          # ровно то, что делает touch
            вердикт_стар = судить_след({"id": "hr.старая", "проверка": {
                "след": str(журнал), "ждём": ['"красных":0'], "часов": 24}})
            проба("БОЛЬНОЙ СЛУЧАЙ: touch не делает старый прогон свежим",
                  "провал", вердикт_стар["исход"])
            проба("в отказе назван возраст записи", True,
                  "80" in вердикт_стар["почему"])

            # Запись без ts: свежесть судить нечем — это не «доказано».
            журнал.write_text('{"режим":"всё","красных":0}\n', encoding="utf-8")
            проба("БОЛЬНОЙ СЛУЧАЙ: в записи нет времени — доказать нечем",
                  "доказать-нечем", судить_след(карточка)["исход"])

            # БОЛЬНОЙ СЛУЧАЙ H1: след, которого нет в объявленных, — негодная
            # форма. Иначе проверяемый пишет доказательство себе сам.
            чужой = стенд / "самодельный.jsonl"
            чужой.write_text(f'{{"ts":"{сейчас}","красных":0}}\n', encoding="utf-8")
            проба("БОЛЬНОЙ СЛУЧАЙ: необъявленный след не принимается", True,
                  "не объявлен" in (негоден_след_почему(
                      {"след": str(чужой), "ждём": ['"красных":0']}) or ""))
            проба("объявленный след форму проходит", None,
                  негоден_след_почему({"след": str(журнал),
                                       "ждём": ['"красных":0']}))

            # БОЛЬНОЙ СЛУЧАЙ: следа нет вовсе — «нечем доказать», не «доказано».
            пропал = стенд / "пропал.jsonl"
            формы.write_text(
                формы.read_text(encoding="utf-8")
                + f"  - путь: \"{пропал}\"\n    пишет: \"стенд\"\n"
                  "    причина: \"проба отсутствующего следа\"\n",
                encoding="utf-8")
            проба("БОЛЬНОЙ СЛУЧАЙ: следа нет — доказать нечем", "доказать-нечем",
                  судить_след({"id": "hr.нет", "проверка": {
                      "след": str(пропал), "ждём": ["x"]}})["исход"])

            # БОЛЬНОЙ СЛУЧАЙ: журнал не JSON. Проба установки пишет обычную
            # строку, начинающуюся датой, — чужой формат под приёмку не
            # переписывают, читают как есть.
            текстовый = стенд / "проба-установки.log"
            формы.write_text(
                формы.read_text(encoding="utf-8")
                + f"  - путь: \"{текстовый}\"\n    пишет: \"стенд\"\n"
                  "    причина: \"проба текстового журнала\"\n",
                encoding="utf-8")
            текстовый.write_text(
                f"{сейчас.replace('T', ' ')}  === проба установки: провалов 0 ===\n",
                encoding="utf-8")
            проба("БОЛЬНОЙ СЛУЧАЙ: журнал не JSON — время берётся из начала строки",
                  "доказано", судить_след({"id": "hr.текст", "проверка": {
                      "след": str(текстовый), "ждём": ["провалов 0"]}})["исход"])

            проба("след вместе с командой — негодная форма", True,
                  негоден_след_почему({"след": str(журнал), "команда": "ls",
                                       "ждём": ["x"]}) is not None)
            проба("след без «ждём» — негодная форма", True,
                  негоден_след_почему({"след": str(журнал)}) is not None)
            проба("относительный путь следа — негодная форма", True,
                  негоден_след_почему({"след": "log/x.jsonl",
                                       "ждём": ["x"]}) is not None)
        finally:
            if прежние_формы is None:
                os.environ.pop("ACCEPTANCE_FORMS", None)
            else:
                os.environ["ACCEPTANCE_FORMS"] = прежние_формы

    # БОЛЬНОЙ СЛУЧАЙ 28.09.2026: задача с пометкой «проверяет владелец» и
    # человеческими шагами числилась долгом СТРОИТЕЛЯ («доказать нечем»), хотя
    # ждала его глаз. Так висели публикация харнеса и ещё три карточки.
    его_задача = {"id": "hr.его", "owner_testable": True,
                  "как_проверить": "откройте панель и посмотрите",
                  "проверка": {"команда": "открыть публичный репозиторий",
                               "ждём": ["версия"]}}
    проба("БОЛЬНОЙ СЛУЧАЙ: owner_testable отправляет задачу в очередь владельца",
          "ждёт-владельца", судить(его_задача, Path.cwd())["исход"])
    # И обратно: без пометки та же карточка остаётся долгом механизма.
    проба("без пометки негодная команда — долг механизма", "доказать-нечем",
          судить({**его_задача, "owner_testable": False,
                  "как_проверить": None}, Path.cwd())["исход"])

    # ── «Сделано, но не заявлено» (план 28.10, пункт 1в) ──────────────────
    # БОЛЬНОЙ СЛУЧАЙ 28.10: 14 карточек с чеклистом 100 %, но закрыты в них
    # следы «Коммит …», а не работа. Кандидат судится по пунктам РАБОТЫ.
    now = 1_800_000_000.0
    day = 86400.0
    commit_items = [{"label": "Коммит a1: правка", "done": True},
                    {"label": "Коммит b2: правка", "done": True}]
    work_done = commit_items + [{"label": "Шаг 1", "done": True}]
    work_open = commit_items + [{"label": "Шаг 1"}]
    stand_cards = [
        {"id": "к.готова", "status": "plan", "checklist": work_done},
        {"id": "к.wip-готова", "status": "wip", "checklist": work_done},
        {"id": "к.работа-открыта", "status": "plan", "checklist": work_open},
        {"id": "к.одни-коммиты", "status": "plan", "checklist": commit_items},
        {"id": "к.свежая", "status": "wip", "checklist": work_done},
        {"id": "к.без-коммитов", "status": "plan", "checklist": work_done},
        {"id": "к.старая", "status": "plan", "checklist": work_done},
        {"id": "к.уже-test", "status": "test", "checklist": work_done},
    ]
    stand_times = {"к.готова": [now - 2 * day], "к.wip-готова": [now - day],
                   "к.работа-открыта": [now - 2 * day],
                   "к.одни-коммиты": [now - 2 * day],
                   "к.свежая": [now - 3 * day, now - 3600],
                   "к.старая": [now - 40 * day], "к.уже-test": [now - 2 * day]}
    found = set(done_but_unclaimed(stand_cards, stand_times, now))
    проба("БОЛЬНОЙ СЛУЧАЙ: закрыты одни «Коммит …», работа открыта — не кандидат",
          False, "к.работа-открыта" in found)
    проба("кандидаты: plan с коммитом и закрытой работой — в списке", True,
          "к.готова" in found)
    проба("кандидаты: wip с закрытой работой — в списке", True, "к.wip-готова" in found)
    проба("кандидаты: без пунктов работы судить не по чему — нет", False,
          "к.одни-коммиты" in found)
    проба("кандидаты: коммит последних часов — работа идёт, нет", False,
          "к.свежая" in found)
    проба("кандидаты: без коммитов задачи — нет", False, "к.без-коммитов" in found)
    проба("кандидаты: коммиты старше окна — нет", False, "к.старая" in found)
    проба("кандидаты: уже test — заявлена, нет", False, "к.уже-test" in found)
    проба("кандидаты: статус машина НЕ меняет", ["plan", "wip"],
          [card["status"] for card in stand_cards
           if card["id"] in ("к.готова", "к.wip-готова")])

    # Времена коммитов читаются из трейлера Dev-Map настоящего git-стенда.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        git_env = {**os.environ, "GIT_AUTHOR_NAME": "p", "GIT_AUTHOR_EMAIL": "p@s",
                   "GIT_COMMITTER_NAME": "p", "GIT_COMMITTER_EMAIL": "p@s",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=git_env)
        for message in ("шаг А\n\nDev-Map: hr.а\nCo-Authored-By: p <p@s>",
                        "шаг Б\n\nDev-Map: hr.б", "без трейлера\n\nтекст Dev-Map: hr.в"):
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
                            "-m", message], check=True, env=git_env)
        parsed = dev_map_commit_times(repo, 30)
    проба("трейлер Dev-Map: обе задачи найдены, строка в тексте — не трейлер",
          ["hr.а", "hr.б"], sorted(parsed or {}))
    проба("нечитаемый git — None, а не «коммитов нет»", None,
          dev_map_commit_times(Path("/nonexistent-acceptance-stand"), 30))

    # 1г: кандидат без проверки — долг строителя заранее; с годной — нет.
    stand_debt = {"lines": [{"id": "harness", "tasks": [
        {"id": "д.кандидат", "status": "plan", "title": "К"},
        {"id": "д.кандидат-с-проверкой", "status": "plan", "title": "КП",
         "проверка": {"команда": "python3 scripts/check-secrets.py", "ждём": "чисто"}},
        {"id": "д.просто-plan", "status": "plan", "title": "П"}]}]}
    taken = {task_id for task_id, _, _ in долг_строителя(
        stand_debt, направление="harness",
        candidates={"д.кандидат", "д.кандидат-с-проверкой"})}
    проба("строитель: кандидат без проверки — берётся", True, "д.кандидат" in taken)
    проба("строитель: кандидат с годной проверкой — НЕ берётся", False,
          "д.кандидат-с-проверкой" in taken)
    проба("строитель: plan не кандидат — НЕ берётся", False, "д.просто-plan" in taken)

    # Границы карточки: правка обязана остаться внутри своей задачи.
    плохо += _проба_границ()

    if плохо:
        print("САМОТЕСТ ПРОВАЛЕН")
        return 1
    # Число путей СЧИТАЕТСЯ, а не держится рукой: рукописное расходилось молча,
    # а реестр берёт его как факт (находка G-19 ревью 27.09.2026).
    print(f"САМОТЕСТ ПРОЙДЕН: {путей[0]} путей, первым — больной случай")
    return 0


def применить(вердикты: list[dict], путь: Path) -> tuple[int, int]:
    """Записать исходы в карту. Правится РОВНО две вещи: статус и дата приёмки.

    Правка текстом, а не перезаписью через yaml: карта — файл, который читают
    люди, и `yaml.dump` переставил бы в ней всё, потеряв порядок, комментарии и
    длинные строки. Заодно это ограничивает власть механизма: он умеет только
    то, что умеет его регулярное выражение.
    """
    from datetime import date
    текст = путь.read_text(encoding="utf-8")
    принято = сегодня = date.today().isoformat()
    закрыто = вернулось = 0
    for в in вердикты:
        ид = re.escape(в["id"])
        # Правка идёт ВНУТРИ границ карточки, а не по всему файлу. Шаблон
        # «(?:\s+\w+:.*\n)*?» проходит поля классом «любой пробел», который
        # включает перевод строки, — и мог перешагнуть в соседнюю карточку.
        # Границы считает общий разбор (ревизия архитектуры 12.09.2026; две
        # аварии этого класса: 88 задач 09.09, 15 задач 12.09).
        найдено = разбор_карточки.блок(текст, в["id"])
        if not найдено:
            continue
        тело, начало_блока, конец_блока = найдено

        def в_карте(новое_тело: str) -> str:
            """Собрать карту с заменённым телом этой карточки."""
            return текст[:начало_блока] + новое_тело + текст[конец_блока:]

        if в["исход"] == "доказано":
            # Поле даты вставляется ТОЛЬКО если его ещё нет. Иначе задача,
            # возвращённая в работу и принятая повторно, получает два
            # `принято_когда`, а YAML берёт последний — то есть прежнюю,
            # неверную дату (известные грабли [[duplicate-yaml-key-is-silent]],
            # находка I3 ревью кода 11.09.2026).
            уже_есть = re.search(r"^\s+принято_когда:", тело, re.M)
            новый = "принято"
            поле = "" if уже_есть else f'\n        принято_когда: "{принято}"'
        elif в["исход"] in ПОМЕТКА_ИСХОДА:
            # Ни один из этих исходов НЕ меняет статус: `task-closer` переводит
            # `wip` старше 48 часов в `plan` с причиной «висела без живого
            # процесса агента» — для задачи, только что возвращённой машиной,
            # это ложь (находка C4 ревью). Исход называется в самой карточке,
            # чтобы его увидели вместе с задачей, а не только в отчёте за ту
            # ночь, который никто не перечитывает (находка I2 ревью кода).
            #
            # Ветка ОДНА на три исхода: отличались они только именем поля, и
            # три копии разъехались бы при первой правке. Имя — в таблице
            # ПОМЕТКА_ИСХОДА, то есть данные.
            #
            # Вывод команды в КАРТУ не пишется: карта лежит в git, а вывод
            # несёт что угодно, включая DSN с паролем (И-3, находка ревью
            # безопасности 27.09.2026; уже пережитое — [[make-печатает-пароль-в-лог]]).
            # Он ложится в $LOG_DIR, где его стережёт check-secret-in-logs.py, а
            # в карточку идёт путь: у строителя есть Read, файл он прочитает.
            имя_поля = ПОМЕТКА_ИСХОДА[в["исход"]]
            причина = str(в.get("почему", ""))
            if в.get("вывод"):
                причина += f"; вывод: {сохранить_вывод(в['id'], str(в['вывод']))}"
            строка = причина[:400].replace('"', "'").replace("\n", " ")
            поле = f'\n        {имя_поля}: "{сегодня}: {строка}"'
            # Пометка ОБНОВЛЯЕТСЯ: прежде негативный lookahead запрещал вторую
            # вставку, и в карточке навсегда оставалось ПЕРВОЕ «в выводе нет
            # X», хотя такого «ждём» там уже нет — строитель правил по
            # несуществующему обещанию (находка G-06 ревью 27.09.2026).
            тело = re.sub(rf"^\s+{имя_поля}:.*\n", "", тело, flags=re.M)
            обр = re.compile(r"(^\s+status: test)", re.M)
            # Замена ФУНКЦИЕЙ, а не строкой: любой обратный слэш в тексте
            # вердикта делал бы шаблон замены невалидным и ронял запись карты
            # целиком (находка ревью безопасности 27.09.2026).
            новое_тело, сколько = обр.subn(
                lambda м: м.group(1) + поле, тело, count=1)
            if сколько:
                текст = в_карте(новое_тело)
                вернулось += 1
            continue
        else:
            continue
        обр = re.compile(r"(^\s+status: )test", re.M)
        новое_тело, сколько = обр.subn(
            lambda м: м.group(1) + новый + поле, тело, count=1)
        if сколько:
            текст = снять_провал(в_карте(новое_тело), в["id"])
            закрыто += в["исход"] == "доказано"
    путь.write_text(текст, encoding="utf-8")
    return закрыто, вернулось


def сохранить_вывод(ид: str, вывод: str) -> str:
    """Положить вывод команды в журнал и вернуть путь к нему.

    В карту он не годится (И-3: карта в git, вывод несёт что угодно), а
    строителю проверок нужен — иначе он снова напишет «ждём» из головы.
    """
    куда = Path(конф.log_dir()) / "priyomka"
    try:
        куда.mkdir(parents=True, exist_ok=True)
        файл = куда / f"{ид}.вывод"
        файл.write_text(вывод, encoding="utf-8")
        return str(файл)
    except OSError as беда:
        # Журнал недоступен — молчать нельзя: без пути строитель не узнает, что
        # печатает команда, и причина в карточке станет бессмысленной.
        return f"(вывод не сохранён: {беда.strerror})"


def снять_провал(текст: str, ид: str) -> str:
    """Убрать пометку провала у задачи, которая доказана.

    Улика 12.09.2026: задача `ev.срок-входа-сторож-11-09` провалила проверку,
    была починена, приёмка её доказала — а поле `провал_проверки` осталось в
    карточке. Прибор очереди ставит провал вторым по важности признаком, и
    доказанная задача висела бы первой строкой «что делать сейчас» вечно.
    Та же грабля, что «пометка снятия не снимается сама»: у пометки обязан
    быть тот, кто её снимает.
    """
    найдено = разбор_карточки.блок(текст, ид)
    if not найдено:
        return текст
    тело, начало, конец = найдено
    имена = "|".join(ПОМЕТКА_ИСХОДА.values())
    без_пометки = re.sub(rf"^\s+({имена}):.*\n", "", тело, flags=re.M)
    return текст[:начало] + без_пометки + текст[конец:]


# Какое поле карточки несёт исход. Таблица ДАННЫЕ: ветки под каждый исход
# расходятся при первой же правке, а поле — единственное, чем они отличались.
ПОМЕТКА_ИСХОДА = {
    "провал": "провал_проверки",          # долг разработчика: сломан механизм
    "проверка-врёт": "проверка_врёт",     # долг строителя: обещание неверно
    "проверка-долгая": "проверка_долгая",  # долг строителя: не уложилась
    "ждёт-мутаций": "мутации_ждут",       # долг до принятия: мутации ночью
}
# Долг СТРОИТЕЛЯ — всё, кроме провала: провал чинит тот, кто писал механизм.
ПОМЕТКИ_СТРОИТЕЛЯ = ("проверка_врёт", "проверка_долгая")


def пометка_долга(задача: dict) -> str | None:
    """Пометка приёмки, за которую отвечает СТРОИТЕЛЬ проверок.

    Их ставит `применить`, и они называют путь к сохранённому выводу. Без такой
    задачи в работе строителя обещание не починит никто: разработчику отдавать
    нечего — механизм-то здоров.
    """
    for поле in ПОМЕТКИ_СТРОИТЕЛЯ:
        если_есть = задача.get(поле)
        if если_есть:
            return f"проверка не сходится с выводом команды — {если_есть}"
    return None


def нет_инструкции(задача: dict) -> str | None:
    """Пометка «может только владелец» без шагов для него — тоже долг строителя.

    Поставив `проверить_владельцу`, строитель эту задачу больше не берёт, и
    поле `как_проверить` не появится никогда: владелец получит объяснение, чего
    не смогла машина, вместо ответа на свой вопрос 27.09.2026 — «что мне нужно
    проверит и как».
    """
    if задача.get("проверить_владельцу") and not задача.get("как_проверить"):
        return ("владельцу нужны ШАГИ: допиши «как_проверить» — что открыть, "
                "что нажать, что он должен увидеть")
    return None


def долг_строителя(карта: dict, направление: str | None = None,
                   candidates: set | frozenset = frozenset()) -> list[tuple[str, str, str]]:
    """Задачи, которые обязан взять строитель: `(id, title, почему)`.

    Отдельно от печати — чтобы проба могла судить ОТБОР, а не текст на экране:
    прежде этот путь не покрывала ни одна из 60 проб судьи, и две мутации в нём
    оставались зелёными (находка G-11 ревью 27.09.2026).

    Направление приходит АРГУМЕНТОМ, а живая настройка — лишь умолчание: пока
    его спрашивали только у конфига, стенд пробы играл на производстве: живое
    направление сменилось, карточек этой линии в стенде нет — и пять путей
    отбора покраснели на верном коде.

    `candidates` — id «сделано, но не заявлено» (`closing_candidates`): им
    проверку строят заранее, чтобы к переводу в `test` она была готова.
    """
    активное = направление if направление is not None else активное_направление()
    итог = []
    for з in карточки(карта, направление=активное or None):
        if з.get("status") != "test" and з.get("id") not in candidates:
            continue
        # Причины СКЛАДЫВАЮТСЯ: у задачи с пометкой «может только владелец»
        # проверка тоже может врать, и тогда её не чинил никто — число
        # владельцу шлётся, а строителю задача не отдаётся (находка G-04).
        # Саму проверку для помеченной задачи строитель не пишет: её ищет
        # только `нет_инструкции`, поэтому за той же порцией он каждую ночь не
        # платит (находка C5 ревью 11.09.2026).
        if з.get("проверить_владельцу"):
            беда = нет_инструкции(з) or пометка_долга(з)
        else:
            беда = негодна_почему(з) or пометка_долга(з)
        if беда:
            итог.append((з.get("id", "?"), str(з.get("title", ""))[:90], беда))
    return итог


# Окно кандидата «сделано, но не заявлено»: коммит задачи был за последние
# CANDIDATE_DAYS суток, и ни одного — за последние QUIET_HOURS часов (работа
# по ней не идёт прямо сейчас).
CANDIDATE_DAYS = 14
QUIET_HOURS = 6
CANDIDATE_STATUSES = ("plan", "wip")


def dev_map_commit_times(root: Path, days: int) -> dict[str, list[float]] | None:
    """Времена коммитов по трейлеру `Dev-Map: <id>` за последние `days`.

    Один вызов git на всю карту. Git не ответил — None: «не прочитали» не
    равно «коммитов нет», и кандидатов тогда не называем вовсе.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", f"--since={days} days ago",
             "--format=%ct%x00%B%x1e"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    found: dict[str, list[float]] = {}
    for record in result.stdout.split("\x1e"):
        stamp, _, body = record.strip().partition("\x00")
        if not stamp.isdigit():
            continue
        for task_id in разбор_карточки.dev_map_ids(body):
            found.setdefault(task_id, []).append(float(stamp))
    return found


def done_but_unclaimed(cards: list[dict], commit_times: dict[str, list[float]], now: float,
                       days: int = CANDIDATE_DAYS, quiet_hours: int = QUIET_HOURS) -> list[str]:
    """id карточек `plan`/`wip`, где вся РАБОТА закрыта, а по задаче затихли коммиты.

    Только список: заявить «сделано» (перевести в `test`) — решение смены,
    машина по косвенному признаку готовность не объявляет (И-4).
    """
    found = []
    for card in cards:
        if card.get("status") not in CANDIDATE_STATUSES:
            continue
        work = формы.work_items(card.get("checklist"))
        if not work or not all(item.get("done") for item in work):
            continue
        recent = [t for t in commit_times.get(card.get("id"), []) if now - t <= days * 86400]
        if recent and now - max(recent) >= quiet_hours * 3600:
            found.append(card["id"])
    return found


def closing_candidates(dev_map: dict) -> list[str] | None:
    """Живые кандидаты активного направления; None — git не прочитан."""
    commit_times = dev_map_commit_times(КОРЕНЬ, CANDIDATE_DAYS)
    if commit_times is None:
        return None
    return done_but_unclaimed(карточки(dev_map, направление=активное_направление() or None),
                              commit_times, time.time())


def _print_closing_candidates() -> int:
    """`id :: title :: подсказка`, последней строкой — «к закрытию: N»."""
    import yaml
    dev_map = yaml.safe_load(КАРТА.read_text(encoding="utf-8"))
    candidates = closing_candidates(dev_map)
    if candidates is None:
        print("к закрытию: ? — git не прочитан, кандидатов не назвать")
        return 1
    titles = {card.get("id"): str(card.get("title", ""))[:90] for card in карточки(dev_map)}
    for task_id in candidates:
        print(f"{task_id} :: {titles.get(task_id, '')} :: переведи в test и впиши «проверка»")
    print(f"к закрытию: {len(candidates)}")
    return 0


def _печать_долга(порция: int = 0) -> int:
    """Печать долга строителя: `id :: title :: почему`, порция сверху."""
    import yaml
    карта = yaml.safe_load(КАРТА.read_text(encoding="utf-8"))
    candidates = set(closing_candidates(карта) or ())
    for номер, (ид, имя, беда) in enumerate(долг_строителя(карта, candidates=candidates), 1):
        print(f"{ид} :: {имя} :: {беда}")
        if порция and номер >= порция:
            break
    return 0


def main() -> int:
    if "--история" in sys.argv:
        names, target = sys.argv[sys.argv.index("--история") + 1], sys.argv[sys.argv.index("--в") + 1]
        return _record_history([n for n in names.split(",") if n], target)
    if "--мутации-задачи" in sys.argv and not os.environ.get("HARNESS_IN_CHECKS_SLICE"):
        # Под потолок среза идёт только настоящий прогон мутаций: пустой набор
        # (нет механизмов, git молчит) дёшев и очередь не занимает.
        # Заявка встаёт в очередь пачек, а не гонит заход сама.
        if any(механизмы_задачи(ид, КОРЕНЬ) for ид in _batch_ids()):
            return _queued_mutations(_batch_ids())
    if "--selftest" in sys.argv:
        return _selftest()
    if "--формы" in sys.argv:
        # Промпт строителя берёт формы ОТСЮДА, а не из головы автора: иначе
        # они расходятся с гейтом молча (12.09.2026 — все 19 написанных
        # проверок оказались запрещёнными, и цикл крутился впустую).
        for форма in ФОРМЫ_СЛОВАМИ:
            print(f"   • {форма}")
        return 0
    if "--мутации-долга" in sys.argv:
        return _ночные_мутации()
    if "--мутации-задачи" in sys.argv:
        return _task_mutations(_batch_ids())
    if "--кандидаты" in sys.argv:
        # «Сделано, но не заявлено»: только список, статус не меняется (И-4).
        return _print_closing_candidates()
    if "--долг" in sys.argv:
        # Работа строителя проверок — ПО МЕРКЕ СУДЬИ, а не по своей. Формы
        # команд он уже берёт отсюда (`--формы`); отбор задач шёл своей мерой
        # «поле проверка есть», и 18 задач с негодной командой не брал никто.
        # Команды здесь НЕ исполняются: судится только форма.
        порция = next((int(а) for а in sys.argv[sys.argv.index("--долг") + 1:]
                       if а.isdigit()), 0)
        return _печать_долга(порция)
    только = None
    if "--задача" in sys.argv:
        после = sys.argv[sys.argv.index("--задача") + 1:]
        названные = [и for и in (после[0].split(",") if после else []) if и]
        if not названные:
            print("--задача требует id карточки: --задача <id>[,<id>]", file=sys.stderr)
            return 2
        только = set(названные)
    import yaml
    карта = yaml.safe_load(КАРТА.read_text(encoding="utf-8"))
    вердикты = разбор(карта, КОРЕНЬ, только)
    if только is not None:
        нет = sorted(только - {в["id"] for в in вердикты})
        if нет:
            print(f"--задача: нет среди карточек в статусе test: {', '.join(нет)}", file=sys.stderr)
            return 2
    итог = свод(вердикты)
    print(json.dumps({"задач в test": len(вердикты), "итог": итог,
                      "очередь владельца": len(очередь_владельца(вердикты)),
                      "долг механизма": len(долг_механизма(вердикты))},
                     ensure_ascii=False))
    for строка in строки_отчёта(вердикты):
        print(строка)
    # Список для владельца пишет ЭТОТ ЖЕ прогон из СВОИХ вердиктов. Отдельный
    # режим, собиравший список прямо из карты, был вторым прибором на тот же
    # факт: как только строитель напишет работающую проверку, число в канале и
    # состав файла разъехались бы (находка ревью безопасности 27.09.2026, тот
    # же класс, что [[two-writers-one-mark-different-thresholds]]).
    # Список пишется только в боевом прогоне: сухой не должен оставлять в дереве
    # неотслеживаемый файл — иначе сторож чужих правок у строителя каждую ночь
    # винит агента (находка G-13 ревью 27.09.2026). Пустая очередь ПЕРЕЗАПИСЫВАЕТ
    # файл: иначе рядом лежал бы вчерашний список как сегодняшний.
    if "--применить" in sys.argv and только is None:
        ждут = очередь_владельца(вердикты)
        СПИСОК_ВЛАДЕЛЬЦУ.parent.mkdir(parents=True, exist_ok=True)
        СПИСОК_ВЛАДЕЛЬЦУ.write_text(
            документ_очереди(ждут, карточки(карта)), encoding="utf-8")
    if "--применить" in sys.argv:
        if только is None:
            закрыто, вернулось = применить(вердикты, КАРТА)
        else:
            # Лок — только в режиме одной карточки: acceptance-watch держит его
            # на fd 9 при целом прогоне, и второй flock там повис бы.
            try:
                with разбор_карточки.map_lock(
                        разбор_карточки.lock_path(),
                        float(os.environ.get("HARNESS_MAP_LOCK_WAIT") or 30)):
                    закрыто, вернулось = применить(вердикты, КАРТА)
            except разбор_карточки.LockBusy as занят:
                print(f"--задача: лок карты занят ({занят}), исходы не записаны", file=sys.stderr)
                return 2
        print(f"записано в карту: принято {закрыто}, вернулось в работу {вернулось}")
    return 1 if итог.get("провал") else 0


if __name__ == "__main__":
    sys.exit(main())
