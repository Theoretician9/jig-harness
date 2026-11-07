#!/usr/bin/env python3
"""Библиотека процессов харнеса: исключения выключателя и строки crontab.

Один модуль, чтобы форма строки крона жила в одном месте: её пишут и
доустановка (install-new.py), и установщик пакета, и живая правка
wrap-crontab. Три копии формата отстали бы друг от друга молча.

CLI:
  process_library.py exempt <id>   rc 0 — процесс исключён из выключателя, 1 — нет
  process_library.py cron-lines <system-jobs.yaml> <проект> <журналы>
      строки crontab всех заданий реестра, по одной в строке (для USTANOVIT.sh)
  process_library.py checks-service <id>   rc 0 — запись идёт службой потока проверок
  process_library.py checks-ids            id таких записей, по одному в строке
  process_library.py checks-units <каталог> <проект> <журналы> <пользователь>
      пишет harness-checks.slice и пары harness-<id>.service/.timer; потолки —
      из $HARNESS_CONF (CHECKS_CPU_QUOTA, CHECKS_MEMORY_HIGH, CHECKS_MEMORY_MAX)
  process_library.py wrap-crontab [--вход <файл>] [--выход <файл>]
      переписывает СВОИ необёрнутые строки crontab в обёрнутые; без --вход
      читает и пишет живой crontab (копия до правки — в $LOG_DIR или /tmp).

Двери проб: HARNESS_PROCESS_LIBRARY — путь к YAML библиотеки (расписание,
выключатель), HARNESS_PROCESS_LIBRARY_JSON — путь к JSON (свойства процессов).
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WRAPPER = "scripts/run-background.sh"
# Поле времени cron: цифры, звёздочка, шаг, список, диапазон.
_ПОЛЯ_ЗАДАНИЯ = ("расписание", "команда", "журнал", "если_задан")
_ПОЛЕ_ВРЕМЕНИ = re.compile(r"^[0-9*/,\-]+$")
# Признак записи JSON: её гонит служба systemd, а не крон, и выключатель её не гасит.
CHECKS_SIGN = "служба-проверок"
CHECKS_SLICE = "harness-checks.slice"
# Директива среза -> ключ harness.conf с её значением.
_CHECKS_CEILINGS = {
    "CPUQuota": "CHECKS_CPU_QUOTA",
    "MemoryHigh": "CHECKS_MEMORY_HIGH",
    "MemoryMax": "CHECKS_MEMORY_MAX",
}
_DOW_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _корень() -> Path:
    # Раскладка проекта и пакета разные: ищем вверх каталог, где лежит harness/config.
    начало = Path(__file__).resolve()
    for каталог in начало.parents:
        if (каталог / "harness" / "config").is_dir():
            return каталог
    return начало.parents[2]


def _каталог_конфигов() -> Path:
    # В пакете корень — сам каталог harness, в проекте — каталог над ним.
    начало = Path(__file__).resolve()
    for каталог in начало.parents:
        if (каталог / "config" / "system-jobs.yaml").is_file():
            return каталог / "config"
    return _корень() / "harness" / "config"


def _прочитать_yaml(путь: Path) -> dict:
    import yaml
    return yaml.safe_load(Path(путь).read_text(encoding="utf-8")) or {}


def load(path=None) -> dict:
    путь = path or os.environ.get("HARNESS_PROCESS_LIBRARY") or _каталог_конфигов() / "process-library.yaml"
    return _прочитать_yaml(Path(путь))


def registry(path=None) -> dict:
    # Свойства процессов (класс, запуск, механизмы, доля нагрузки) живут в JSON, расписание — в YAML.
    import json
    путь = path or os.environ.get("HARNESS_PROCESS_LIBRARY_JSON") or _каталог_конфигов() / "process-library.json"
    return json.loads(Path(путь).read_text(encoding="utf-8"))


def is_exempt(process_id: str, path=None) -> bool:
    try:
        исключения = (load(path).get("выключатель") or {}).get("исключения") or []
    except (OSError, ValueError):
        return False
    return any(запись.get("id") == process_id for запись in исключения)


def jobs(path=None) -> list:
    """Задания крона в форме system-jobs. Без довода — из `процессы:` библиотеки:
    записи без расписания (sentinel живёт под таймером) заданиями не считаются.
    С доводом — реестр в прежней форме (`задания:` system-jobs.yaml): так зовёт
    USTANOVIT.sh пакета."""
    if path:
        return _прочитать_yaml(Path(path)).get("задания") or []
    return [{"имя": имя, **{поле: запись[поле] for поле in _ПОЛЯ_ЗАДАНИЯ if поле in запись}}
            for имя, запись in (load().get("процессы") or {}).items() if "расписание" in запись]


def checks_ids(path=None) -> list:
    """id записей с признаком «служба-проверок» — по JSON, расписание живёт в YAML."""
    return [запись["id"] for запись in registry(path).get("процессы") or []
            if запись.get("запуск") == CHECKS_SIGN]


def _calendar_field(spec: str, first=0, names=None) -> str:
    """Поле крона → поле OnCalendar. first — первое значение поля: «*/n» у systemd
    не принимается и пишется «<first>/n»; у дня месяца и месяца отсчёт с 1.
    Дни недели (names) раскрываются в список имён: диапазон «0-2» через воскресенье
    у systemd не склеивается, а список даёт те же срабатывания, что в кроне."""
    def one(part):
        body, sep, step = part.partition("/")
        if names:
            low, _, high = body.partition("-")
            return ",".join(names[i] for i in range(int(low), int(high or low) + 1))
        if body == "*" and step:
            body = str(first)
        return body.replace("-", "..") + sep + step
    return ",".join(one(part) for part in spec.split(","))


def on_calendar(cron: str) -> str:
    """Пять полей крона → OnCalendar. Крон при заданных днях месяца и недели
    срабатывает по любому из них, у systemd это И: такое расписание не переводим."""
    minute, hour, dom, month, dow = cron.split()
    if dom != "*" and dow != "*":
        raise ValueError(f"расписание «{cron}»: день месяца и день недели вместе в systemd значат иное")
    if "/" in dow:
        raise ValueError(f"расписание «{cron}»: шаг в дне недели не переводится")
    day = "" if dow == "*" else _calendar_field(dow, names=_DOW_NAMES) + " "
    return (f"{day}*-{_calendar_field(month, 1)}-{_calendar_field(dom, 1)} "
            f"{_calendar_field(hour)}:{_calendar_field(minute)}:00")


# Как запускается запись без расписания крона — словами владельцу.
_START_WORDS = {
    "хук": "при событии сессии (хук)", "служба": "постоянно", "контейнер": "постоянно",
    "загрузка": "при загрузке сервера", "таймер": "по таймеру systemd",
    "служба-проверок": "по таймеру службы проверок",
    "вызов-модели": "по ходу работы сессии", "агент-сессии": "по ходу работы сессии",
    "команда-сессии": "по ходу работы сессии",
}
_DOW_WORDS = ("воскресеньям", "понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям")


def _plural(count: int, forms: tuple) -> str:
    """forms: (1, 2-4, 5+) — «минута, минуты, минут»."""
    if count % 100 in range(11, 15) or count % 10 not in range(1, 5):
        return forms[2]
    return forms[0] if count % 10 == 1 else forms[1]


def _step(field: str):
    """«*/n» → n, иначе None."""
    return int(field[2:]) if re.fullmatch(r"\*/\d+", field) else None


def _number(field: str) -> bool:
    return re.fullmatch(r"\d{1,2}", field) is not None


def _minute(field: str) -> bool:
    return _number(field) and int(field) < 60


def schedule_words(cron, start=None) -> str:
    """Расписание крона словами; без расписания — по виду запуска. Что не разобрано —
    сама строка с пометкой «как в кроне»: молчать или падать на чужой строке нельзя."""
    if not cron:
        return _START_WORDS.get(start, "по событию")
    fields = str(cron).split()
    if len(fields) == 5 and fields[3] == "*":
        minute, hour, dom, _, dow = fields
        if (minute, hour, dom, dow) == ("*", "*", "*", "*"):
            return "каждую минуту"
        if _step(minute) and (hour, dom, dow) == ("*", "*", "*"):
            return f"каждые {_step(minute)} {_plural(_step(minute), ('минуту', 'минуты', 'минут'))}"
        if _minute(minute) and (hour, dom, dow) == ("*", "*", "*"):
            return f"каждый час в :{int(minute):02d}"
        if _minute(minute) and _step(hour) and (dom, dow) == ("*", "*"):
            return f"каждые {_step(hour)} {_plural(_step(hour), ('час', 'часа', 'часов'))} в :{int(minute):02d}"
        if _minute(minute) and _number(hour) and int(hour) < 24:
            clock = f"{int(hour):02d}:{int(minute):02d}"
            if (dom, dow) == ("*", "*"):
                return f"каждый день в {clock}"
            if dom == "*" and re.fullmatch(r"[0-7]", dow):
                return f"по {_DOW_WORDS[int(dow)]} в {clock}"
            if _number(dom) and dow == "*":
                return f"{int(dom)}-го числа каждого месяца в {clock}"
    return f"{cron} (как в кроне)"


def _checks_conf() -> dict:
    # Под `python3 -I` каталога скрипта в sys.path нет.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    return config.из_файла(config.путь_harness_conf())


def checks_units(project: str, logs: str, user: str, conf=None) -> dict:
    """{имя файла: текст} среза и пар service+timer. Расписание и команда — из
    библиотеки, потолки — из harness.conf; ключа нет — ошибка, а не молчаливое
    умолчание: потолок, выбранный тут, разошёлся бы с конфигом."""
    conf = _checks_conf() if conf is None else conf
    нет = [ключ for ключ in _CHECKS_CEILINGS.values() if not conf.get(ключ)]
    if нет:
        raise KeyError(f"в harness.conf нет ключей потолка: {', '.join(нет)}")
    ceilings = "".join(f"{директива}={conf[ключ]}\n" for директива, ключ in _CHECKS_CEILINGS.items())
    units = {CHECKS_SLICE: ("[Unit]\nDescription=Поток проверок харнеса: общий потолок ядер и памяти\n\n"
                            f"[Slice]\n{ceilings}")}
    processes = load().get("процессы") or {}
    for pid in checks_ids():
        запись = processes[pid]
        calendar = on_calendar(запись["расписание"])
        units[f"harness-{pid}.service"] = (
            f"[Unit]\nDescription=Харнес: {pid} (поток проверок)\n\n"
            "[Service]\nType=oneshot\n"
            f"User={user}\nSlice={CHECKS_SLICE}\n"
            f"ExecStart={project}/{WRAPPER} {pid} -- {project}/{запись['команда']}\n"
            f"StandardOutput=append:{logs}/{запись['журнал']}\n"
            "StandardError=inherit\n")
        units[f"harness-{pid}.timer"] = (
            f"[Unit]\nDescription=Харнес: расписание {pid}\n\n"
            f"[Timer]\nOnCalendar={calendar}\n"
            "Persistent=false\n"
            f"Unit=harness-{pid}.service\n\n"
            "[Install]\nWantedBy=timers.target\n")
    return units


def _выключено_ключом(job: dict, conf: dict) -> bool:
    return bool(job.get("если_задан")) and not conf.get(job["если_задан"])


def cron_line(job: dict, project: str, logs: str) -> str:
    return (f"{job['расписание']}  {project}/{WRAPPER} {job['имя']} -- "
            f"{project}/{job['команда']} >>{logs}/{job['журнал']} 2>&1")


def cron_lines(project: str, logs: str, conf=None, jobs_list=None) -> list:
    """Строки crontab заданий. conf (словарь или текст harness.conf): задание с
    пустым `если_задан` не ставится; conf=None — ставятся все (установщик пакета
    сверяет crontab со всем реестром). Сам выключатель состав не меняет: строки
    стоят всегда, гасит ключ."""
    if isinstance(conf, str):
        import config
        conf = config.из_текста(conf)
    checks = checks_ids()
    return [cron_line(job, project, logs)
            for job in (jobs() if jobs_list is None else jobs_list)
            if job["имя"] not in checks
            if conf is None or not _выключено_ключом(job, conf)]


def split_cron_line(line: str):
    """(приставка, время, команда, хвост) или None, если это не строка задания.

    Приставка — ведущий `#` с необязательной меткой («#ВЫКЛ-0511 »); хвост —
    перенаправление вывода со своими пробелами. Строка собирается обратно
    конкатенацией, чужие байты не теряются."""
    m = re.match(r"^(\s*#\s*(?:[^\s0-9*@/,\-]\S*\s+)?)(.*)$", line, re.S)
    приставка, остаток = (m.group(1), m.group(2)) if m else ("", line)
    закомментирована = bool(m)
    if закомментирована:
        поля = остаток.split(None, 5) if not остаток.startswith("@") else остаток.split(None, 1)
        время_полей = 1 if остаток.startswith("@") else 5
        if len(поля) < время_полей + 1 or not all(
                _ПОЛЕ_ВРЕМЕНИ.match(п) for п in поля[:время_полей] if not п.startswith("@")):
            return None
    elif line.strip() == "":
        return None
    if остаток.startswith("@"):
        m2 = re.match(r"^(@\S+\s+)(.*)$", остаток, re.S)
    else:
        m2 = re.match(r"^((?:\S+\s+){5})(.*)$", остаток, re.S)
    if not m2:
        return None
    время, остаток = m2.group(1), m2.group(2)
    m3 = re.match(r"^(.*?)(\s*>>?.*)?$", остаток, re.S)
    команда, хвост = m3.group(1), m3.group(2) or ""
    return приставка, время, команда.strip(), хвост


def wrap_crontab(text: str, project: str, job_list=None) -> tuple:
    """(новый текст, сколько строк обёрнуто). Оборачиваются только строки,
    чья команда ЦЕЛИКОМ равна `<project>/<команда>` известного задания."""
    известные = {" ".join(f"{project}/{j['команда']}".split()): j
                 for j in (jobs() if job_list is None else job_list)}
    итог, обёрнуто = [], 0
    for строка in text.splitlines(keepends=True):
        тело = строка.rstrip("\n")
        разбор = split_cron_line(тело)
        задание = известные.get(" ".join(разбор[2].split())) if разбор else None
        if not задание:
            итог.append(строка)
            continue
        приставка, время, команда, хвост = разбор
        новая = f"{приставка}{время}{project}/{WRAPPER} {задание['имя']} -- {команда}{хвост}"
        итог.append(новая + строка[len(тело):])
        обёрнуто += 1
    return "".join(итог), обёрнуто


def _cli_wrap(argv: list) -> int:
    вход = выход = None
    i = 0
    while i < len(argv):
        if argv[i] == "--вход" and i + 1 < len(argv):
            вход = argv[i + 1]; i += 2
        elif argv[i] == "--выход" and i + 1 < len(argv):
            выход = argv[i + 1]; i += 2
        else:
            print(f"wrap-crontab: неизвестный довод {argv[i]}", file=sys.stderr)
            return 2
    проект = os.environ.get("PROJECT_DIR") or str(_корень())
    if вход:
        было = Path(вход).read_text(encoding="utf-8")
    else:
        готово = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
        было = готово.stdout if готово.returncode == 0 else ""
    стало, число = wrap_crontab(было, проект)
    if выход:
        Path(выход).write_text(стало, encoding="utf-8")
    elif not вход and число:
        копия = Path(os.environ.get("LOG_DIR") or "/tmp") / f"crontab.before-wrap-{time.strftime('%Y%m%d-%H%M%S')}"
        копия.write_text(было, encoding="utf-8")
        готово = subprocess.run(["crontab", "-"], input=стало, text=True,
                                capture_output=True, timeout=30)
        if готово.returncode != 0:
            print(f"wrap-crontab: crontab не записан: {готово.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"wrap-crontab: копия до — {копия}")
    print(f"wrap-crontab: обёрнуто строк {число}")
    return 0


def main(argv: list) -> int:
    if len(argv) == 2 and argv[0] == "exempt":
        return 0 if is_exempt(argv[1]) else 1
    if len(argv) == 4 and argv[0] == "cron-lines":
        print("\n".join(cron_lines(argv[2], argv[3], None, jobs(argv[1]))))
        return 0
    if len(argv) == 2 and argv[0] == "checks-service":
        return 0 if argv[1] in checks_ids() else 1
    if argv == ["checks-ids"]:
        print("\n".join(checks_ids()))
        return 0
    if len(argv) == 5 and argv[0] == "checks-units":
        каталог = Path(argv[1])
        каталог.mkdir(parents=True, exist_ok=True)
        for имя, текст in checks_units(argv[2], argv[3], argv[4]).items():
            (каталог / имя).write_text(текст, encoding="utf-8")
        return 0
    if argv and argv[0] == "wrap-crontab":
        return _cli_wrap(argv[1:])
    print("process_library.py exempt <id> | cron-lines <реестр> <проект> <журналы> | checks-service <id> | "
          "checks-ids | checks-units <каталог> <проект> <журналы> <пользователь> | wrap-crontab [--вход Ф] [--выход Ф]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
