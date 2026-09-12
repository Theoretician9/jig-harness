#!/usr/bin/env python3
# Взят из UNIFIED/templates/hooks/guard_bash.py; добавлено: чтение /etc/harness/install.conf,
# И-1 (drop database / rm -r по каталогам данных и бэкапов), И-2 (docker cp в живой контейнер),
# ворота deploy_guard.py перед git push и deploy.sh. По ревью 09.08.2026: закрыты обходы
# В-2 (env-префиксы, timeout/nohup/xargs/env, bash -c, docker-compose, путь-префиксы,
# git -C push, find -delete, docker cp контейнер→контейнер) и ложные срабатывания В-3
# (COMPOSE_RESTART и PYTEST заякорены на позицию команды).
"""Запрет на команды, которые уже ломали работу.

Хук PreToolUse на Bash. Читает вызов из stdin, при совпадении с известной бедой
возвращает код 2 — харнес отменяет вызов и показывает причину модели.

Здесь только то, что реально случалось. Защита от воображаемых бед прячет ту,
которая бывает, поэтому список короткий и каждый пункт назван своей историей.
Ложный запрет хуже пропуска: от него избавляются обходом всей защиты.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

CONF_PATH = "/etc/harness/install.conf"
HARNESS_CONF_PATH = "/etc/harness/harness.conf"
# Значения по умолчанию — сторож обязан работать и там, где конфига ещё нет
# (свежая машина, тестовый прогон): всё, что зависит от конфига, при его
# отсутствии молчит (fail-open), а не блокирует.
CONF_DEFAULTS = {
    "AUTONOMY": "semi",
    "HEARTBEAT_DIR": "/var/lib/harness/heartbeat",
    "LOG_DIR": "/var/log/harness",
}


def read_conf(path: str | None = None) -> dict:
    """Простой парсер shell-конфига вида KEY="value" (или KEY=value).

    Не source: сторожу нельзя исполнять чужой код, ему нужны только значения.
    Переменная окружения HARNESS_INSTALL_CONF переопределяет путь — так тесты
    подсовывают временный конфиг, не трогая /etc.
    """
    conf = dict(CONF_DEFAULTS)
    path = path or os.environ.get("HARNESS_INSTALL_CONF") or CONF_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"""\s*([A-Z][A-Z0-9_]*)=("[^"]*"|'[^']*'|[^#\s]*)""", line)
                if m:
                    raw = m.group(2)
                    conf[m.group(1)] = raw[1:-1] if raw[:1] in "\"'" else raw
    except OSError:
        pass  # конфига нет — работаем с дефолтами (fail-open, см. требование выше)
    return conf


def log_fail_open(reason: str, detail: str) -> None:
    """След каждого fail-open в $LOG_DIR/guard_exceptions.log (JSONL).

    Молчаливый fail-open неотличим от здорового сторожа: команда прошла — а
    почему, никто не видел. След читает heartbeat-watch и называет число
    падений в утренней сводке. Best-effort: отказ записи (нет прав, нет
    каталога) не ломает сам fail-open — блокировать работу из-за лога нельзя.
    """
    try:
        log_dir = read_conf().get("LOG_DIR") or "/var/log/harness"
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "guard_exceptions.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "reason": reason, "detail": detail},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass


# Команда узнаётся ТОЛЬКО в позиции команды: в начале строки, после разделителя
# или внутри подстановки. Иначе хук срабатывает на тексте, который про эту команду
# лишь рассказывает, — так он заблокировал запись собственной документации в первый
# же день. Запрет, мешающий писать о запрете, снимают вместе со всей защитой.
# Обратная кавычка в позицию команды НЕ входит: как подстановка она устарела и в
# наших командах не встречается, зато ею размечают код в markdown — и сторож ловил
# `команду` в тексте документации, мешая эту документацию писать.
# ОТСТУП — тоже позиция команды. Дыра, найденная 10.09.2026 при ревью: якорь
# «^» без \s* не видел ни одной команды внутри if, цикла или функции, а таких
# в скриптах большинство. Замер на живом стороже до правки:
#   «    docker compose restart api»            → пропущено (rc=0)
#   «if true; then docker compose restart api»  → пропущено (rc=0)
#   «docker compose restart api»                → отменено  (rc=2)
# То есть весь сторож обходился одним пробелом в начале строки. Ключевые слова
# then/do/else/{ добавлены по той же причине: после них идёт команда.
# Позиция команды. Ключевые слова перечислены ВСЕ, какие ведут за собой команду:
# 10.09.2026 замер показал, что «if docker compose restart api; then …»,
# «while …», «! …» и «time …» проходили мимо ВСЕХ правил сторожа — ловились
# только формы через then/do. Это та же дыра, что накануне нашлась с отступом:
# запрет знал одну запись команды из нескольких.
_CMD_POS = (r"(?:^\s*|[;&|(]\s*|\$\(\s*"
            r"|\b(?:then|do|else|elif|if|while|until|time)\s+"
            r"|!\s*|\{\s*)")
# Прозрачные префиксы (В-2): сквозь них команда остаётся командой. Ревьюер живьём
# провёл «LC_ALL=C rm -rf /opt/app/data», «timeout 60 claude», «nohup claude mcp»,
# «... | xargs rm -rf» мимо сторожа — каждый вид префикса здесь по этой причине.
_PRE = (
    # VAR=val (в т.ч. несколько подряд). Значение бывает В КАВЫЧКАХ, и «\S*»
    # на пробеле внутри них обрывался: замер 10.09.2026 — «LANG='ru RU' docker
    # compose restart api» и «IFS=$'\n' read -r файл» проходили мимо ВСЕХ правил.
    r"(?:(?:[A-Za-z_][A-Za-z_0-9]*=(?:\$?'[^']*'|\"[^\"]*\"|\S)*"
    r"|sudo(?:\s+-\S+)*"
    r"|nohup"
    r"|setsid(?:\s+-\S+)*"
    # Отложенный запуск — те же руки, только через минуту. Пробой 12.08.2026
    # «systemd-run --on-active=45 … tmux kill-server» прошёл мимо сторожа:
    # команда стояла не в позиции команды, а аргументом. Короткие флаги с
    # аргументом перечислены поимённо: «-\S+ \S+» съел бы саму команду.
    r"|systemd-run(?:\s+(?:-[uUpMEG]\s+\S+|-{1,2}\S+))*"
    r"|env(?:\s+-\S+)*"
    r"|timeout(?:\s+-\S+)*\s+[\d.]+[smhd]?"   # timeout [флаги] N[s|m|h|d]
    r"|xargs(?:\s+-\S+)*"                     # цели придут по stdin — см. rm ниже
    r")\s+)*"
)
# Путь-префиксы (В-2): «/usr/bin/dropdb» — та же команда, что и «dropdb».
_BIN = r"(?:/usr/local/s?bin/|/usr/s?bin/|/s?bin/)?"
_POS = _CMD_POS + _PRE + _BIN
# Обёртки, сквозь которые команда остаётся командой: sudo и docker [compose] exec.
# Без этого «docker compose exec db psql -c "DROP DATABASE ..."» проходил мимо.
_WRAP = r"(?:sudo\s+)?(?:docker\s+(?:compose\s+)?exec\s+(?:-\S+\s+)*\S+\s+)?"
# Флаги (возможно с аргументом) между именем команды и подкомандой:
# «git -C /path push», «docker compose -f file.yml restart» (В-2).
_FLAGS = r"(?:-{1,2}\S+(?:\s+[^-\s]\S*)?\s+)*"

# Беда — НЕ «MCP включён», а «на токене появился второй потребитель
# getUpdates»: он убивает живой канал с владельцем. Первая редакция правила
# требовала пустого набора MCP от ЛЮБОГО запуска claude и вместе с телеграмом
# отключала все остальные серверы. Владелец 11.08.2026: «надо только телеграм
# убрать». Запрет сужен до своей причины — гейт под случившийся отказ, а не
# под воображаемый.
#
# 1. Телеграм-канал плагина: ровно он и забирает getUpdates.
TELEGRAM_CHANNEL = re.compile(
    _POS + r"claude\s+(?:\S+\s+)*?--channels[=\s]\S*telegram", re.MULTILINE
)
# 2. Телеграм среди серверов переданного набора MCP.
TELEGRAM_MCP = re.compile(
    _POS + r"claude\s+(?:\S|\s)*?--mcp-config[^\n]*telegram", re.MULTILINE
)
# 3. Правка состава MCP: включённый однажды телеграм переживёт сессию.
#    Чтение (list, get) безобидно и разрешено — запрет читать состав только
#    заставлял бы лезть в файлы руками.
MCP_MUTATE = re.compile(
    _POS + r"claude\s+mcp\s+"
    r"(?:add|add-json|add-from-claude-desktop|remove|enable|disable|reset-project-choices)\b",
    re.MULTILINE,
)
# Заякорено на позицию команды (В-3): незаякоренный паттерн блокировал
# «echo '...docker compose restart...'» и grep по документации.
# docker-compose (v1) — синоним; флаги между compose и restart — тоже он (В-2).
COMPOSE_RESTART = re.compile(
    _POS + r"docker(?:-compose|\s+compose)\s+" + _FLAGS + r"restart\b", re.MULTILINE
)
# Чистка образов «всё, что не запущено»: `docker image prune -a` / `docker
# system prune -a` сносят и `:previous` — снимок, на который откатывается
# выкат при мёртвом health (И-2, scripts/deploy.sh делает его перед сборкой).
# Улика 10.09.2026: агент убирал по слову владельца 3,5 ГБ мусора, взял форму
# с -a и вместе с десятью висячими образами снёс app-api:previous. Прод при
# этом жив, данные целы — но страховки выката не стало, и вернуть её нечем:
# образ не восстанавливается. Висячие образы чистятся БЕЗ -a и этим правилом
# не задеваются.
PRUNE_ALL = re.compile(
    _POS + r"docker\s+" + _FLAGS + r"(?:image|system)\s+prune\b[^\n;|&]*"
    r"(?:\s-[a-zA-Z]*a[a-zA-Z]*(?![a-zA-Z-])|\s--all\b)",
    re.MULTILINE,
)
# Тот же необратимый итог достигается ещё двумя путями: прямым удалением образа
# отката. Ревью 10.09.2026 показало на живом: `docker rmi app-api:previous` и
# `docker image rm …:previous` проходили мимо правила, хотя ломают ровно то же —
# страховку выката (scripts/deploy.sh держит откат на «:previous»).
RMI_PREVIOUS = re.compile(
    _POS + r"docker\s+" + _FLAGS + r"(?:rmi|image\s+rm)\b[^\n;|&]*:previous\b",
    re.MULTILINE,
)
# Имя переменной кириллицей: bash берёт в идентификаторы только латиницу,
# цифры и подчёркивание. Команда не падает молча — она печатает В ОШИБКУ САМО
# ЗНАЧЕНИЕ («ПУТЬ=/секрет: command not found»), и 12.08.2026 так утёк токен
# бота. За смену 25.08.2026 агент наступил трижды подряд (замер сценария,
# копирование скриптов, замер холодного старта) — правило в памяти есть, но
# решение, которое требует помнить, решением не является.
# Ловим и `for ИМЯ in`: то же самое, только через цикл.
#
# Ищем ТОЛЬКО в том, что исполнит сам bash: тела heredoc вырезаются
# (`heredoc_вырезан`). Внутри них чужой язык — в этом проекте python с русскими
# именами, где кириллица законна: первая редакция правила отменила правку
# журнала на строке `строка = (...)`. Ложный запрет хуже пропуска: от него
# избавляются обходом всей защиты.
# `=` без пробела — признак присваивания оболочки: python по PEP 8 пишет с
# пробелами, а `[ "$A" == "да" ]` отсекается требованием одиночного знака.
# Кириллица ГДЕ УГОДНО в имени, а не только первой буквой: 09.09.2026 имя
# `DSN_СТЕНД` прошло мимо прежней редакции (она требовала кириллицу в начале),
# bash имя не взял и напечатал в ошибку САМО ЗНАЧЕНИЕ — строку подключения с
# паролем. Опережающая проверка требует хотя бы одну русскую букву в имени.
_КИР_ИМЯ = (r"((?=[A-Za-zА-Яа-яЁё0-9_]*[А-Яа-яЁё])"
            r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)")

КИРИЛЛИЦА_В_ИМЕНИ = re.compile(
    _CMD_POS + r"(?:for\s+)?" + _КИР_ИМЯ + r"(?:=(?!=)|\s+in\s)",
    re.MULTILINE,
)

# Второй путь к тому же нарушению: объявляющие команды получают имя АРГУМЕНТОМ,
# без знака «=», и правило присваивания его не видит. 10.09.2026 так прошли
# `read -r файл …` и `local -a слова` — bash ответил «not a valid identifier» на
# каждой строке, цикл не выполнился ни разу, а мутационная проба отчиталась
# зелёной, не проверив ни одного механизма. Латинские имена в списке
# пропускаются: ловится первое кириллическое, где бы в перечне оно ни стояло.
КИРИЛЛИЦА_В_ОБЪЯВЛЕНИИ = re.compile(
    _CMD_POS + _PRE + r"(?:local|declare|typeset|export|readonly|read)\s+"
    r"(?:-[A-Za-z]+\s+)*(?:[A-Za-z_][A-Za-z0-9_]*(?:=\S*)?\s+)*" + _КИР_ИМЯ + r"(?:\s|;|$)",
    re.MULTILINE,
)


def имя_кириллицей(текст: str):
    """Первое кириллическое имя переменной — присвоенное или объявленное.

    Один экземпляр правила на оба пути (команда в оболочке и файл из
    редактора): два списка форм разъезжаются молча, и разошедшаяся половина
    молчит ровно тогда, когда нужна.
    """
    for правило in (КИРИЛЛИЦА_В_ИМЕНИ, КИРИЛЛИЦА_В_ОБЪЯВЛЕНИИ):
        найдено = правило.search(текст)
        if найдено:
            return найдено
    return None


# Арифметика `$(( 1 << N ))` — не heredoc, а сдвиг. Вырезается до поиска
# маркера: замер 11.09.2026 показал, что «SHIFT=$(( 1 << N ))» объявляло всё
# до конца команды телом heredoc и снимало ВСЕ запреты сторожа разом.
_АРИФМЕТИКА = re.compile(r"\$\(\(.*?\)\)", re.DOTALL)


def heredoc_вырезан(cmd: str) -> str:
    """Команда без тел heredoc: там не bash, и правила оболочки к ним не
    относятся. Маркер берётся из самой команды, конец — строка с ним.

    Открытие ищется в тексте БЕЗ кавычек и без арифметики: «<<» бывает и
    внутри строкового литерала («grep -n "<<EOF"»), и в сдвиге. До 11.09.2026
    поиск шёл по сырой строке — и любое такое упоминание ослепляло сторожа
    целиком: `grep -n "<<EOF" f.sh` + `docker compose restart api` проходило
    (rc=0), та же команда без первой строки отменялась. Восьмая форма обхода
    из найденных за сутки и самая широкая: «<<» в командах встречается часто.
    """
    открыт = re.compile(r"<<-?\s*[\"']?([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)")
    # Маска той же длины, что и команда: в ней погашены строковые литералы и
    # арифметика. Маркер ищем в ОРИГИНАЛЕ — у `<<'PY'` кавычки часть
    # конструкции, и гашение съело бы сам маркер, — а по маске проверяем
    # ПОЗИЦИЮ: если на месте «<<» в маске пробел, это упоминание внутри
    # литерала или арифметический сдвиг, а не открытие heredoc.
    маска = _АРИФМЕТИКА.sub(lambda m: " " * len(m.group(0)), кавычки_погашены(cmd))
    итог, ждём, сдвиг = [], None, 0
    for строка in cmd.splitlines():
        if ждём is None:
            итог.append(строка)
            for найден in открыт.finditer(строка):
                место = сдвиг + найден.start()
                if место < len(маска) and маска[место] == "<":
                    ждём = найден.group(1)
                    break
        elif строка.strip() == ждём:
            ждём = None
        сдвиг += len(строка) + 1
    return "\n".join(итог)
# Тоже заякорено (В-3): «grep -rn pytest docs/» — не прогон тестов.
# Путь к интерпретатору тут ЛЮБОЙ, а не только системный: прогоны идут
# `.venv/bin/python -m pytest`, и _BIN (только /usr/bin и соседи) их не видел —
# правило «прогон уже идёт» молчало на всех настоящих командах проекта.
PYTEST_CALL = re.compile(
    _CMD_POS + _PRE + _WRAP + _PRE + r"(?:[\w.~-]*/)*(?:python[\d.]*\s+-m\s+)?pytest\b",
    re.MULTILINE,
)

# И-1: уничтожение данных. dropdb — команда сама по себе; drop database/schema/table —
# только вместе с клиентом БД в позиции команды, иначе ловили бы текст про SQL.
DROPDB_CMD = re.compile(_CMD_POS + _PRE + _WRAP + _PRE + _BIN + r"dropdb\b", re.MULTILINE)
DB_CLIENT = re.compile(
    _CMD_POS + _PRE + _WRAP + _PRE + _BIN + r"(?:psql|mysql|mariadb|mongosh?)\b", re.MULTILINE
)
DROP_SQL = re.compile(r"\bdrop\s+(?:database|schema|table)\b", re.IGNORECASE)
# В-2-остаток: «psql -f drop.sql» проходил — SQL лежит в файле и сторожу не
# виден, а там может быть drop database. Узкий fail-closed (И-1): клиент БД
# с -f/--file в позиции команды — отказ; -c с явным SQL и текст — проходят.
DB_FILE_FLAG = re.compile(
    _CMD_POS + _PRE + _WRAP + _PRE + _BIN
    + r"(?:psql|mysql|mariadb)\b[^|;&()]*\s(?:-f|--file)(?:[=\s]|$)",
    re.MULTILINE,
)

# И-1: rm с рекурсией по каталогам данных/бэкапов. Захватываем аргументы rm до
# ближайшего разделителя и смотрим компоненты путей.
RM_CMD = re.compile(_POS + r"rm\s+([^|;&()]*)", re.MULTILINE)
# И-1: find -delete — то же уничтожение, другой глагол (обход из ревью В-2).
FIND_CMD = re.compile(_POS + r"find\s+([^|;&()]*)", re.MULTILINE)
PROTECTED_DIRS = {"data", "backup", "backups", "secrets"}

# И-2: docker cp ВНУТРЬ контейнера (второй аргумент вида имя:путь) — ручной выкат.
# Копирование ИЗ контейнера наружу (логи, дампы) не запрещено; источник может быть
# и контейнерным (В-2: «docker cp a:b backend:/x» — тоже выкат внутрь).
DOCKER_CP_IN = re.compile(
    _POS + r"docker\s+cp\s+(?:-\S+\s+)*\S+\s+[^\s:]+:\S+", re.MULTILINE
)

# Ворота деплоя: git push и запуск deploy.sh сначала спрашивают deploy_guard.py.
# Флаги между git и push: «git -C /path push» — тот же push (В-2).
GIT_PUSH = re.compile(_POS + r"git\s+" + _FLAGS + r"push\b", re.MULTILINE)
DEPLOY_SH = re.compile(_CMD_POS + _PRE + r"(?:(?:bash|sh)\s+)?\S*deploy\.sh\b", re.MULTILINE)

# Завершение своей сессии мимо ротации. Улика 11.08.2026, 22:51: агент подал
# себе «/exit» в панель и вышел, а поднять преемника умеет только сторож —
# смена не состоялась, харнес встал до вмешательства владельца руками.
# Уход разрешён ровно один: session-warden.sh --rotate-now (выход + подъём).
TMUX_KILL = re.compile(
    _POS + r"tmux\s+" + _FLAGS + r"kill-(?:session|server|window|pane)\b([^|;&()]*)", re.MULTILINE
)
TMUX_SEND = re.compile(
    _POS + r"tmux\s+" + _FLAGS + r"send-keys\b([^|;&()]*)", re.MULTILINE
)
# Нагрузка, завершающая агента: команда выхода TUI и клавиши обрыва.
PANE_QUIT_PAYLOAD = re.compile(r"(?:/exit\b|\bC-c\b|\bC-d\b|\bq\s*!)", re.IGNORECASE)
KILL_CLAUDE = re.compile(
    _POS + r"(?:pkill|killall)\b[^|;&()]*\bclaude\b", re.MULTILINE
)


def ротация_ведётся_сторожем(now: float | None = None) -> bool:
    """Смену уже ведёт session-warden и ждёт РОВНО моего выхода?

    Пережито 12.08.2026. Сторож подал в панель поручение «заверши по регламенту
    и выйди командой /exit; НЕ зови --rotate-now: смену веду я». Единственный
    способ исполнить — послать себе /exit, а этот сторож его отменял: правило
    «не уходи сам» не различало, кто заказал уход. Получалось поручение,
    исполнить которое нельзя ничем: `--rotate-now` встал бы в очередь за локом
    ведущего прогона и заблокировал обоих (авария 13:20 того же дня).

    Признак берётся из журнала ротаций, а НЕ из «бежит ли процесс сторожа»:
    плановый прогон тоже виден в ps, но преемника не поднимет — выйти по нему
    значило бы уйти без смены, то есть ровно та беда 11.08 в 22:51, ради
    которой запрет и написан. Та же логика и тот же порог, что у
    session-warden (`ротация_в_ходу`): два прибора должны отвечать одинаково.
    """
    conf = read_conf()
    путь = os.path.join(conf.get("LOG_DIR") or "/var/log/harness", "rotation.jsonl")
    порог = int(conf.get("ROTATE_FINISH_TIMEOUT_SEC") or 900)
    try:
        with open(путь, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            хвост = fh.read()[-8192:] if fh.tell() > 8192 else None
        if хвост is None:
            with open(путь, "rb") as fh:
                хвост = fh.read()
        последнее = None
        for строка in хвост.decode("utf-8", "replace").splitlines():
            try:
                событие = json.loads(строка)
            except ValueError:
                continue            # обрезанная первая строка хвоста — не наш контракт
            if событие.get("event"):
                последнее = событие
        if not последнее or последнее.get("event") != "rotate_command":
            return False
        подан = time.mktime(time.strptime(последнее["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
        return 0 <= ((now if now is not None else time.time()) - подан) < порог
    except Exception:
        # Журнала нет или он нечитаем — запрет остаётся в силе. Fail-closed
        # именно здесь: ошибиться в сторону «выходить нельзя» стоит одного
        # лишнего вопроса, а в другую — остановки харнеса до рук владельца.
        return False

# В-2: нагрузка bash -c '...' / sh -c "..." — команда, разбирается рекурсивно.
SHELL_C = re.compile(
    _CMD_POS + _PRE + _BIN + r"(?:bash|sh|dash|zsh)\s+(?:-\S+\s+)*-\w*c\s+('[^']*'|\"[^\"]*\"|\S+)",
    re.MULTILINE,
)


def _ancestors() -> set[int]:
    """Свои процессы вверх по дереву.

    Без этого проверка «идёт ли уже прогон тестов» ловит саму себя: команда,
    которую хук разбирает, лежит в его собственной командной строке, и поиск по
    строке находит слово pytest у родительской оболочки. Поймано тестом хука.
    """
    seen, pid = set(), os.getpid()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            pid = int(open(f"/proc/{pid}/stat").read().split(") ", 1)[1].split()[1])
        except Exception:
            break
    return seen


def running_pytest() -> list[str]:
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    mine = _ancestors()
    found = []
    for line in out.stdout.splitlines():
        pid_s, _, args = line.strip().partition(" ")
        if not pid_s.isdigit() or int(pid_s) in mine:
            continue
        if re.search(r"(^|/)(python[\d.]*\s+-m\s+pytest|pytest)\b", args):
            found.append(pid_s)
    return found


def _protected_component(token: str) -> bool:
    parts = [p.lower() for p in token.strip("'\"").split("/") if p]
    return any(p in PROTECTED_DIRS for p in parts)


def _strip_comment(text: str) -> str:
    """Хвостовой комментарий (# после пробела или в начале) — вон.

    П-10: слово в комментарии — не цель команды; «xargs rm -rf  # чистка data/x»
    блокировался из-за data/ в комментарии. Резка грубая (# внутри кавычек тоже
    режется), но для поиска целей это безопасно: недосмотр сужает скан, а # в
    середине настоящего пути (без пробела перед ним) не трогается.
    """
    return re.sub(r"(?:^|\s)#.*", "", text)


def _statement_around(cmd: str, pos: int) -> str:
    """Оператор, содержащий позицию pos: границы — ;, &, перевод строки.

    П-10: цели rm, поданные по stdin, ищутся в ЭТОМ операторе (включая сегменты
    конвейера слева от rm — оттуда цели и приходят), а не по всей строке:
    слово из соседней команды или комментария — не цель rm.
    """
    start = max(cmd.rfind(c, 0, pos) for c in ";&\n") + 1  # rfind: -1 → 0
    ends = [e for e in (cmd.find(c, pos) for c in ";&\n") if e != -1]
    end = min(ends) if ends else len(cmd)
    return _strip_comment(cmd[start:end])


def rm_protected_target(cmd: str) -> str | None:
    """Путь из rm-с-рекурсией, задевающий каталог данных/бэкапов, или None."""
    for m in RM_CMD.finditer(cmd):
        tokens = _strip_comment(m.group(1)).split()
        paths = [t for t in tokens if not t.startswith("-")]
        # Рекурсия НЕ обязательна: «rm -f .../secrets/tg_bot_token» уносит секрет
        # так же безвозвратно, как «rm -rf .../secrets». Улика приёмки 11.08.2026:
        # сторож молча пропустил удаление файла с токеном бота внутри
        # SECRETS_DIR — правило спрашивало про глагол, а И-1 про потерю данных.
        for t in paths:
            if _protected_component(t):
                return t.strip("'\"")
        if not paths:
            # rm без явных целей — их подаёт stdin (find ... | xargs rm -rf, В-2):
            # смотрим пути в операторе с этим rm (П-10) — источник целей слева
            # от конвейера, а комментарии и соседние команды уже отрезаны.
            for t in re.split(r"""[\s'"]+""", _statement_around(cmd, m.start())):
                if not t.startswith("-") and _protected_component(t):
                    return t
    return None


def find_delete_target(cmd: str) -> str | None:
    """Путь из find ... -delete, задевающий каталог данных/бэкапов, или None (В-2)."""
    for m in FIND_CMD.finditer(cmd):
        # комментарий отрезается той же резкой, что у rm (П-10): «# ... -delete»
        # в хвосте — рассказ про удаление, не удаление
        tokens = [t.strip("'\"") for t in _strip_comment(m.group(1)).split()]
        if "-delete" not in tokens:
            continue
        for t in tokens:
            if not t.startswith("-") and _protected_component(t):
                return t
    return None


# eval исполняет свою строку ровно как оболочка, но `-c` у него нет, и под
# SHELL_C он не подходил. Замер 09.09.2026: `bash -c "docker compose restart x"`
# сторож отменял, а `eval "docker compose restart x"` пропускал — запрет И-2
# обходился одним словом. Дыра касалась ВСЕХ правил разом, не только этого.
EVAL_C = re.compile(
    _CMD_POS + _PRE + _BIN + r"eval\s+('[^']*'|\"[^\"]*\"|\S+)",
    re.MULTILINE,
)


def nested_shell_payloads(cmd: str) -> list[str]:
    """Нагрузки bash -c '...' / sh -c "..." / eval "..." — разбираются как команды (В-2)."""
    out = []
    for правило in (SHELL_C, EVAL_C):
        for m in правило.finditer(cmd):
            raw = m.group(1)
            out.append(raw[1:-1] if raw[:1] in "'\"" else raw)
    return out


def кавычки_погашены(текст: str) -> str:
    """Содержимое кавычек → пробелы, СКВОЗЬ переводы строк.

    Внутри кавычек лежит не bash, а текст сообщений и чужие языки: 09.09.2026
    сторож трижды отменил законное — питон с русскими именами в `python3 -c`,
    сообщения вида «(гейты=$N)». Нагрузки `bash -c` и `eval` при этом не
    теряются: их разбирает nested_shell_payloads отдельной командой.
    Применяется ТОЧЕЧНО, правилом кириллических имён: остальные правила ищут
    имена команд, а не присваивания, и им гашение не нужно.
    """
    итог, кавычка, экран = [], None, False
    for символ in текст:
        if экран:
            # Экранированный символ не закрывает кавычку и не открывает её.
            # Без этого одна «\"» внутри двойных кавычек переворачивала разбор:
            # текст становился «кодом», код — «текстом», и правила слепли или
            # отменяли законное (замер 11.09.2026).
            итог.append(символ if кавычка is None else
                        ("\n" if символ == "\n" else " "))
            экран = False
            continue
        if символ == "\\" and кавычка != "'":
            # В одинарных кавычках bash обратный слэш не считает экраном.
            итог.append(символ if кавычка is None else " ")
            экран = True
            continue
        if кавычка is None:
            итог.append(символ)
            if символ in "\"'":
                кавычка = символ
        elif символ == кавычка:
            итог.append(символ)
            кавычка = None
        else:
            итог.append("\n" if символ == "\n" else " ")
    return "".join(итог)


def deploy_gate_busy(действие: str = "deploy") -> str | None:
    """Спросить ворота. Возвращает причину «занято» или None (путь свободен).

    `действие` — «push» либо «deploy»: у отправки в репозиторий и у выката
    разные предметы согласия владельца, и «да» про одно не разрешает другого.
    Улика 03.09.2026: хук звал ворота БЕЗ действия, они считали всё выкатом, и
    push встал при разрешении, данном словами «на гит отправляй, да».

    Ворот нет (нет конфига, нет PROJECT_DIR, нет скрипта) или ворота сломаны —
    отвечаем None: fail-open, сломанные ворота не имеют права останавливать работу.
    """
    project_dir = read_conf().get("PROJECT_DIR", "")
    if not project_dir:
        return None
    guard = os.path.join(project_dir, "scripts", "deploy_guard.py")
    if not os.path.isfile(guard):
        return None
    try:
        p = subprocess.run(["python3", guard, "--действие", действие],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        # fail-open: ошибка самих ворот — не повод блокировать; след — в лог
        log_fail_open("сломанные ворота deploy_guard", repr(e))
        return None
    if p.returncode != 0:
        return (p.stdout + p.stderr).strip() or "ворота ответили «занято» без пояснения"
    return None


def pack_bans(cmd: str) -> str | None:
    """Запрет от УСТАНОВЛЕННОЙ возможности или None.

    Возможность (harness/vozmozhnosti/<id>/) может принести свои запреты файлом
    `запреты.list`: строки «РЕГЭКСП<TAB>СООБЩЕНИЕ». Читаются только у тех
    возможностей, у которых стоит метка `.установлено`, — пока набор не
    поставлен, сторож этих строк не видит и лишних запретов не появляется.
    Это тот же принцип, что у скилов возможностей: механизм, а не намерение.

    Fail-open со следом: битый регэксп, нечитаемый файл, отсутствие каталога —
    команда проходит, причина уходит в guard_exceptions.log. Сломанный
    дополнительный запрет не имеет права останавливать работу.
    """
    project_dir = read_conf().get("PROJECT_DIR", "")
    if not project_dir:
        return None
    root = os.path.join(project_dir, "harness", "vozmozhnosti")
    if not os.path.isdir(root):
        return None
    try:
        packs = sorted(os.listdir(root))[:20]  # потолок: сторож бежит на каждой команде
    except OSError as e:
        log_fail_open("не читается каталог возможностей", repr(e))
        return None
    for pack in packs:
        list_path = os.path.join(root, pack, "запреты.list")
        if not os.path.isfile(os.path.join(root, pack, ".установлено")):
            continue
        if not os.path.isfile(list_path):
            continue
        try:
            lines = open(list_path, encoding="utf-8").read().splitlines()[:50]
        except OSError as e:
            log_fail_open(f"не читается {list_path}", repr(e))
            continue
        for n, line in enumerate(lines, 1):
            if not line.strip() or line.lstrip().startswith("#") or "\t" not in line:
                continue
            pattern, _, message = line.partition("\t")
            try:
                if re.search(pattern, cmd, re.MULTILINE):
                    return (message.replace("\\n", "\n")
                            + f"\n(запрет возможности «{pack}», строка {n} запреты.list)")
            except re.error as e:
                log_fail_open(f"битый регэксп в {list_path}:{n}", repr(e))
                continue
    return None


def _tmux_target(args: str) -> str | None:
    """Значение -t у команды tmux; None — цели нет (значит, текущая сессия)."""
    m = re.search(r"-t\s*=?\s*(\"[^\"]*\"|'[^']*'|\S+)", args)
    if not m:
        return None
    return m.group(1).strip("\"'")


_TMUX_SOCKET = re.compile(r"-([LS])\s*=?\s*(\"[^\"]*\"|'[^']*'|\S+)")


def _other_server(cmd: str) -> bool:
    """Обращена ли команда tmux к ДРУГОМУ серверу, где панели агента нет.

    Агент поднят без -L/-S, то есть на сокете по умолчанию. Проба на своём
    сокете (`tmux -L проба kill-server`) его сессии не касается — а запрет
    на неё запрещал ровно ту проверку аварии «tmux слетел целиком», ради
    которой сторож и написан (пережито 12.08.2026).

    Сокет из подстановки неизвестен — значит свой: так же 11.08 пустая
    переменная в -t увела команду в собственную сессию.
    """
    m = _TMUX_SOCKET.search(cmd)
    if not m:
        return False
    socket = m.group(2).strip("\"'")
    if not socket or "$" in socket:
        return False
    return socket.rstrip("/").rsplit("/", 1)[-1] != "default"


def _hits_agent_session(args: str) -> bool:
    """Бьёт ли команда tmux по сессии агента.

    Цели нет — бьёт по текущей, а текущая для агента своя. Цель с подстановкой
    ($S) сторожу неизвестна: именно так 11.08 и вышло — переменную читали из
    того файла, где её нет, она была пуста, и «-t ''» ушло в свою же сессию.
    """
    target = _tmux_target(args)
    if target is None or "$" in target or target == "":
        return True
    session = read_conf(os.environ.get("HARNESS_CONF") or HARNESS_CONF_PATH).get(
        "TMUX_SESSION", "agent"
    )
    return target == session


def session_suicide(cmd: str) -> str | None:
    """Причина запрета на завершение сессии агента, или None."""
    if KILL_CLAUDE.search(cmd):
        return "процесс агента снимается сигналом (pkill/killall claude)"
    for m in TMUX_KILL.finditer(cmd):
        if not _other_server(m.group(0)) and _hits_agent_session(m.group(1)):
            return "tmux kill-* закрывает панель агента"
    for m in TMUX_SEND.finditer(cmd):
        if _other_server(m.group(0)):
            continue
        if PANE_QUIT_PAYLOAD.search(m.group(1)) and _hits_agent_session(m.group(1)):
            return "в панель агента подаётся команда выхода"
    return None


# Прогон тестов — только фоном. Указание владельца 23.08.2026: «механизм не
# позволяющий запускать прогон внутри сессии». Улика того же дня: `make smoke`
# шёл 40 минут внутри сессии, ротация оборвала его на 73 % («make: ***
# [Makefile:226: smoke] Terminated») — счёт в мусор, красные неразобраны;
# до того та же беда трижды уносила прогон вместе с логом
# ([[фоновая-работа-умирает-с-сессией]]).
ФОНОМ = re.compile(_CMD_POS + _PRE + r"(?:setsid|systemd-run)\b", re.MULTILINE)
# Сборка тестов (--collect-only) — две секунды и не прогон; она стоит первой
# строкой самих прогонов, запрещать её значит запретить проверку перед ними.
БЕЗ_ПРОГОНА = re.compile(r"--collect-only|--version|--help\b")
MAKE_ПРОГОН = re.compile(
    _CMD_POS + _PRE + _WRAP + _PRE + _BIN + r"make\s+" + _FLAGS +
    r"(?:smoke|test|прогон-быстрый|прогон-живой|тест-стенд-движок)\b",
    re.MULTILINE,
)
ДОЛГИЙ_КОЛЛЕКТОР = re.compile(
    _CMD_POS + _PRE + _WRAP + _PRE + r"(?:[\w.~-]*/)*python[\d.]*\s+(?:-\S+\s+)*"
    r"collectors/progon_dvizhka\.py",
    re.MULTILINE,
)
# Заякорено на позицию команды, как и всё остальное (В-3): имя скрипта в тексте —
# не запуск. Незаякорённая редакция отменила правку журнала, где этот запуск
# всего лишь упомянут (23.08.2026, поймано в тот же час).
# Узкая цель — конкретные файлы набора: соло-прогон упавшего теста идёт секунды
# и переживает ход, ради него правило не заводилось.
ФАЙЛ_НАБОРА = re.compile(r"\btests?/[^\s;&|()]+\.py(?:::[^\s;&|()]+)?")
КАТАЛОГ_НАБОРА = re.compile(r"\btests?/(?=[\s;&|()\"']|$)")


def прогон_в_сессии(cmd: str) -> str | None:
    """Долгий прогон, запущенный НЕ фоном, — причина отказа или None."""
    if ФОНОМ.search(cmd):
        return None
    предмет = ""
    if MAKE_ПРОГОН.search(cmd):
        предмет = "цель make с прогоном тестов"
    elif ДОЛГИЙ_КОЛЛЕКТОР.search(cmd):
        предмет = "прогон сезона движка (~18 минут)"
    else:
        m = PYTEST_CALL.search(cmd)
        if not m:
            return None
        # От КОНЦА совпадения: начало заякорено на разделитель (`&&`, `;`), и
        # _statement_around, считая границы от него, отдавал ПУСТОЙ оператор —
        # аргументы в него не попадали, и законный соло-прогон после `&&`
        # отменялся как прогон всего набора (поймано 23.08.2026, первым же разбором).
        стмт = _statement_around(cmd, m.end())
        if БЕЗ_ПРОГОНА.search(стмт):
            return None
        файлы = ФАЙЛ_НАБОРА.findall(стмт)
        if файлы and len(файлы) <= 3 and not КАТАЛОГ_НАБОРА.search(стмт):
            return None
        предмет = "прогон набора тестов"
    return (
        f"Внутри сессии запускается {предмет} — так нельзя.\n\n"
        "Ход сессии кончается вместе с ней: ротация, компакт или падение обрывают\n"
        "прогон на середине. 23.08.2026 `make smoke` умер на 73 % — сорок минут\n"
        "счёта в мусор, а красные так и остались неразобранными.\n\n"
        "Запускать фоном, лог — вне сессии, ждать по ИТОГОВОЙ СТРОКЕ лога:\n"
        "  setsid nohup <команда> > /var/log/harness/<имя>.log 2>&1 < /dev/null &\n\n"
        "Соло-прогон конкретного теста (до трёх файлов набора) не запрещён."
    )


# ── ожидание, ловящее само себя ──────────────────────────────────────────────
# Снимок владельца 09.09.2026: пять фоновых ожиданий `until ! pgrep -f "<шаблон>"`
# не кончались ничем, «/exit» упирался в вопрос «Background work is running»,
# которого владелец нажать не может — сессия висела.
# Причина: шаблон стоит в командной строке САМОГО ожидающего процесса, и pgrep
# находит себя. Замер того же дня: `pgrep -f` по заведомо несуществующему имени
# вернул свою же оболочку, ожидание висело по таймауту (код 124); тот же шаблон
# через класс символов завершился за секунду (код 0).
# Правило проверяет не форму записи, а само условие: шаблон применяется как
# регулярное выражение к тексту команды. Совпал — pgrep найдёт себя.
# Случай третий с 28.08.2026; три записи в памяти не помогли, потому что помнить
# приходилось человеку.
# Ровно в условии цикла эта ошибка и живёт, а «until ! » — не позиция команды по
# _CMD_POS: без этих префиксов правило ловило разовый вызов и пропускало вечное
# ожидание, ради которого заводилось.
_УСЛОВИЕ = r"(?:(?:until|while|if|elif|do|then|else)\s+|!\s*)*"
PGREP_CALL = re.compile(
    _CMD_POS + _УСЛОВИЕ + _PRE + _BIN + r"(pgrep|pkill)\s+([^\n;&|()]*)",
    re.MULTILINE,
)
# Флаги pgrep, забирающие следующий токен: иначе `pgrep -u fred pytest` принял бы
# за шаблон имя пользователя.
PGREP_ФЛАГ_С_АРГУМЕНТОМ = {
    "-d", "-F", "-g", "-G", "-P", "-s", "-t", "-u", "-U",
    "--delimiter", "--pgroup", "--group", "--parent", "--session",
    "--terminal", "--euid", "--uid", "--pidfile", "--ns", "--nslist",
}


def _pgrep_шаблон(аргументы: str) -> tuple[str, bool] | None:
    """Шаблон вызова pgrep и признак «сравнивается командная строка» (-f)."""
    по_командной_строке = False
    токены = аргументы.split()
    пропустить = False
    for токен in токены:
        if пропустить:
            пропустить = False
            continue
        if not токен.startswith("-") or токен == "-":
            return токен.strip("\"'"), по_командной_строке
        if токен in PGREP_ФЛАГ_С_АРГУМЕНТОМ:
            пропустить = True
        if токен == "--full" or (not токен.startswith("--") and "f" in токен[1:]):
            по_командной_строке = True
    return None


def ожидание_ловит_себя(cmd: str) -> str | None:
    """`pgrep -f`, чей шаблон совпадает с самой командой, — причина отказа."""
    for m in PGREP_CALL.finditer(cmd):
        разбор = _pgrep_шаблон(m.group(2))
        if not разбор:
            continue
        шаблон, по_командной_строке = разбор
        if not по_командной_строке or not шаблон:
            continue
        try:
            if not re.search(шаблон, cmd):
                continue
        except re.error:
            continue  # не наше дело разбирать чужой синтаксис
        инструмент = m.group(1)
        убивает = "\nИ pkill по такому шаблону убьёт саму эту команду.\n" if инструмент == "pkill" else ""
        return (
            f"Шаблон «{шаблон}» совпадает с текстом самой команды: {инструмент} -f сравнивает\n"
            "КОМАНДНЫЕ СТРОКИ, а шаблон стоит в командной строке этого же процесса.\n"
            f"Он найдёт себя — и ответит «процесс жив», даже когда работы давно нет.{убивает}\n"
            "Улика 09.09.2026 (снимок владельца): пять таких ожиданий не кончились\n"
            "ничем, выход сессии упёрся в вопрос о фоновых задачах, которого владелец\n"
            "нажать не может. Замер: pgrep по заведомо несуществующему имени вернул\n"
            "свою же оболочку, ожидание висело по таймауту.\n\n"
            "Ждать работу так:\n"
            "  until ! kill -0 <pid> 2>/dev/null; do sleep 30; done   # по PID запуска\n"
            '  until grep -qE "passed|failed|error" <лог>; do sleep 30; done  # по итогу\n'
            "Разовая проверка живости — классом символов, он ломает самосовпадение:\n"
            f"  pgrep -af \"[{шаблон[0]}]{шаблон[1:]}\""
        )
    return None


# ── ожидание без предела времени ────────────────────────────────────────────
# Замер по журналам 12.09.2026: 96 раз сессия не закрывалась на вопросе о
# фоновых задачах, и в списке КАЖДЫЙ раз стояло десять ожиданий вида
# «until grep -q <строка> <лог>; do sleep …; done». Такое ожидание не кончается
# никогда, если строки не будет: прогон упал, лог не создан, имя другое.
# Дальше цепочка шла сама — тревога сторожа, перезапуск, выпадение в оболочку,
# простой смены; ротацию чинили трижды, а держало выход вот это.
#
# Ожидание по PID (`kill -0`) предела не требует: процесс кончится в любом
# исходе, и цикл выйдет сам. Предел нужен там, где условие зависит от ТЕКСТА,
# которого может не появиться.
ЦИКЛ_ОЖИДАНИЯ = re.compile(
    r"\b(until|while)\b(?P<условие>[^\n;]*);?\s*do\b(?P<тело>.*?)\bdone\b",
    re.DOTALL,
)
ПРЕДЕЛ = re.compile(r"\btimeout\s+\d+|\bSECONDS\b|\bdate\s+\+%s\b")
# Цикл читает КОНЕЧНЫЙ вход — файл, вывод команды, список. Он кончится сам,
# и пауза внутри не делает его ожиданием (ревью кода 12.09.2026: правило
# отменяло обычный разбор файла с паузой между строками).
КОНЕЧНЫЙ_ВХОД = re.compile(r"\bread\b")


def ожидание_без_предела(cmd: str, предел_снаружи: bool = False) -> str | None:
    """Цикл со `sleep`, чьё условие ждёт ТЕКСТА, обязан иметь предел времени.

    Предел ищется в куске строки ПЕРЕД самим циклом, а не по всей команде:
    «timeout 5 echo привет; until …» снимало правило со следующего цикла,
    хотя предела ему никто не давал (ревью кода 12.09.2026).
    """
    for m in ЦИКЛ_ОЖИДАНИЯ.finditer(cmd):
        условие, тело = m.group("условие"), m.group("тело")
        if предел_снаружи or ПРЕДЕЛ.search(cmd[:m.start()].rsplit(";", 1)[-1]):
            continue      # предел стоит перед этим циклом или на обёртке
        if КОНЕЧНЫЙ_ВХОД.search(условие):
            continue      # читает конечный вход — кончится сам
        if not re.search(r"\bsleep\s", тело):
            continue          # цикл без паузы — обработка, а не ожидание
        # Ожидание ПРОЦЕССА кончится в любом исходе — процесс завершится сам.
        # Предел нужен там, где условие ждёт ТЕКСТА, которого может не быть.
        if re.search(r"\bkill\s+-0\b|\bpgrep\b|\bpidof\b", условие):
            continue
        return (
            "Ожидание без предела времени: цикл ждёт условия, которого может\n"
            "не наступить НИКОГДА — прогон упал, лог не создан, строка другая.\n"
            "Такое ожидание живёт до конца сессии и копится.\n\n"
            "Замер по журналам 12.09.2026: 96 раз выход сессии упирался в вопрос\n"
            "о фоновых задачах, и в списке каждый раз было десять таких ожиданий.\n"
            "Владелец этот вопрос нажать не может: дальше шли тревога, перезапуск,\n"
            "выпадение в оболочку и простой смены.\n\n"
            "Дать предел:\n"
            "  timeout 600 bash -c 'until grep -qE \"passed|failed\" <лог>; do sleep 30; done'\n"
            "Либо ждать по PID запуска — он кончится в любом исходе:\n"
            "  until ! kill -0 <pid> 2>/dev/null; do sleep 30; done"
        )
    return None


# Формы, в которых секрет попадает в командную строку. Первая редакция знала
# две (DSN и KEY=значение) и пропускала главную — токен бота в ПУТИ URL
# (api.telegram.org/bot<токен>/sendMessage), а также Bearer, -p/--password и
# «-u user:pass» (ревью 10.09.2026: токен уехал бы в журнал отмен целиком).
ТАЙНОЕ = re.compile(
    r"(://[^:@\s/]+:)[^@\s]+(@)"                       # DSN: user:pass@host
    r"|((?:PASS|PASSWORD|PASSWD|TOKEN|SECRET|APIKEY|API_KEY|KEY)\s*[=:]\s*)\S+"
    r"|(/bot)[0-9]+:[A-Za-z0-9_-]+"                     # Telegram Bot API в пути
    r"|((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]{8,}"      # заголовок авторизации
    r"|(\s-p)[^\s-]\S*"                                 # mysql -pСЕКРЕТ
    r"|(\s--password[= ])\S+"
    r"|(\s-u\s+[^\s:]+:)\S+",                          # curl -u user:pass
    re.IGNORECASE)


def без_тайн(текст: str) -> str:
    """Пароль из DSN и значение PASS=/TOKEN= — под звёздочки.

    Журнал отмен живёт вне репозитория, но его читает девятый гейт
    (`check-sekret-v-logah.py`), и он прав: пароль боевой базы уже уезжал в
    логи 257 раз через рецепты make. Пишем то, что нужно для счёта и разбора,
    а не всю командную строку.
    """
    def замена(m: "re.Match") -> str:
        if m.group(1):                       # DSN: сохраняем «user:» и «@»
            return m.group(1) + "***" + m.group(2)
        # Остальные формы: оставляем распознанный префикс, значение — под звёзды.
        for g in (3, 4, 5, 6, 7, 8):
            if m.group(g):
                return m.group(g) + "***"
        return "***"

    return ТАЙНОЕ.sub(замена, текст)


# Команда, которую сторож разбирает прямо сейчас: ставится в check() и нужна
# только следу. Передавать её вторым параметром в deny значило бы тронуть все
# двадцать с лишним мест отказа ради одной строки журнала.
ПРОВЕРЯЕМАЯ = ""


# ── печать значения секрета в вывод ──────────────────────────────────────────
# Улика 12.09.2026: проверяя, доходит ли долгий токен до оболочек, агент
# написал `echo "${CLAUDE_CODE_OAUTH_TOKEN:+есть}${CLAUDE_CODE_OAUTH_TOKEN:-НЕТ}"`.
# Вторая подстановка печатает ЗНАЧЕНИЕ, когда переменная непуста, — токен на
# год ушёл в вывод и осел в двух транскриптах (шесть вхождений). Затирать
# пришлось задним числом; сам секрет при этом лежал в файле 0600, то есть
# правило «секреты вне репо» соблюдалось, а дыра была в ПЕЧАТИ.
#
# Правило одно на все формы: подстановка секретной переменной запрещена, кроме
# тех форм, которые значения напечатать не могут, — длина `${#VAR}` и признак
# `${VAR:+…}`. Частные случаи «echo», «printf», «env» не перечисляются: значение
# утекает любым выводом, а перечисление команд — это список дыр, а не правило.
СЕКРЕТНОЕ_ИМЯ = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|CREDENTIAL")
# Имена, где то же слово означает ПУТЬ, ЧИСЛО или ИМЯ, а не сам секрет:
# TG_TOKEN_FILE — путь к файлу, CTX_WINDOW_TOKENS — размер окна, SECRETS_DIR —
# каталог. Прогон правила по всему репозиторию до включения дал 59 совпадений в
# 18 файлах, и почти все были из этих трёх видов: краснеющий без причины сторож
# отключают, поэтому граница проведена до включения, а не после жалоб.
НЕ_СЕКРЕТ = re.compile(r"(?:_(?:FILE|DIR|PATH|REPORT|LOG|URL|NAME|ID|VAR|GLOB)$)|TOKENS")
_ПОДСТАНОВКА = re.compile(r"\$\{([^}]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_ИМЯ_И_ФОРМА = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(.*)", re.DOTALL)


def печать_секрета(cmd: str) -> str | None:
    """Имя секретной переменной, чьё ЗНАЧЕНИЕ команда способна напечатать."""
    for найдено in _ПОДСТАНОВКА.finditer(cmd):
        тело, простое = найдено.group(1), найдено.group(2)
        if простое is not None:
            имя, форма = простое, ""
        else:
            if тело.startswith("#"):
                continue                      # ${#VAR} — длина, не значение
            разбор = _ИМЯ_И_ФОРМА.match(тело)
            if not разбор:
                continue
            имя, форма = разбор.group(1), разбор.group(2)
            if форма.startswith(("+", ":+")):
                continue                      # ${VAR:+есть} — признак, не значение
        if СЕКРЕТНОЕ_ИМЯ.search(имя) and not НЕ_СЕКРЕТ.search(имя):
            return имя
    return None


def log_deny(reason: str, cmd: str) -> None:
    """След КАЖДОЙ отмены в $LOG_DIR/guard.jsonl.

    Улика 10.09.2026 (ревизия харнеса): за всю жизнь сторожа в его журнале две
    записи — обе про собственные сбои, а сами отмены не писались нигде. В тот
    же день он дважды остановил безобидные команды (`grep` с именем скрипта
    выката в шаблоне, `grep` со словом pytest), и доказать это можно было
    только памятью агента. Без счёта отмен нельзя ответить на вопрос, от
    которого зависят пороги: где сторож спасает, а где мешает.

    Best-effort: сбой записи не должен мешать отмене — правило важнее следа.
    """
    # Прогон собственного теста — не событие эксплуатации. За первый час журнал
    # набрал 137 отмен, из них ~120 наделал test_guard_bash: его пути без
    # конфига (NOCONF_CASES) падают на боевой LOG_DIR по умолчанию. Метрика
    # «сколько раз сторож мешал» врала бы в разы — а ради неё журнал и заведён.
    if os.environ.get("HARNESS_GUARD_NOLOG") == "1":
        return
    try:
        log_dir = read_conf().get("LOG_DIR") or "/var/log/harness"
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "guard.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "verdict": "deny",
                # Первая строка причины — имя правила человеческим языком.
                "rule": без_тайн(reason.strip().splitlines()[0][:120]),
                "argv0": (cmd.strip().split() or [""])[0][:40],
                "cmd_head": без_тайн(cmd.strip()[:120]),
                "cmd_len": len(cmd),
            }, ensure_ascii=False) + "\n")
        # След для следующей команды: по нему пишется исход (log_followup).
        os.makedirs(os.path.dirname(_след_файл()), exist_ok=True)
        # Токены — из МАСКИРОВАННОЙ строки: в cmd_head пароль закрыт, а сюда
        # он ложился открытым (ревью 10.09.2026, И-3). Права 0600: файл живёт
        # в каталоге, читаемом всем, и переживает конец сессии.
        след = os.open(_след_файл(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(след, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(),
                       "rule": без_тайн(reason.strip().splitlines()[0][:120]),
                       "tokens": sorted(_токены(без_тайн(cmd)))}, fh, ensure_ascii=False)
    except Exception:
        pass


# Чья это сессия. Хук получает session_id в JSON вызова; без него — PPID
# оболочки. Ревью 10.09.2026: след отмены был ОДИН на машину, и исход чужой
# отмены приписывался моей следующей команде (в журнале за один час нашлось
# четыре таких пары). Метрика «помог сторож или помешал» врала именно в том
# режиме, в котором харнес и живёт, — несколько сессий и субагентов разом.
СЕССИЯ = ""


def _след_файл() -> str:
    """Состояние процесса, а не журнал: живёт в HEARTBEAT_DIR, не в LOG_DIR.

    Ревью 10.09.2026: файл лежал в $LOG_DIR и не подпадал ни под одну маску
    гейта удержания — тот краснел бы всякий раз, когда между отменой и коммитом
    не успела пройти следующая команда. Имя с точки: обход меток в sentinel
    берёт «*», скрытые файлы в отчёт не попадают.
    """
    conf = read_conf()
    каталог = conf.get("HEARTBEAT_DIR") or "/var/lib/harness/heartbeat"
    кто = re.sub(r"[^A-Za-z0-9_-]", "", СЕССИЯ)[:32] or str(os.getppid())
    return os.path.join(каталог, f".guard_last.{кто}")


def _токены(cmd: str) -> set:
    return {t for t in re.split(r"[^\w./-]+", cmd.lower()) if len(t) > 1}


def log_followup(cmd: str) -> None:
    """Чем кончилась ПРЕДЫДУЩАЯ отмена: повторил, переиначил или отступил.

    Замечание владельца 10.09.2026 к ревизии: журнал отмен без последствия
    отвечает, сколько раз сторож сработал, и не отвечает, помог он или помешал.
    Мера — доля общих слов со следующей командой: почти та же команда значит
    «правило не приняли», другая — «нашёл иной путь», ничего общего — «отступил».

    Одна отмена — одна запись: файл-след удаляется сразу, иначе каждая команда
    сутками писала бы followup к одной и той же отмене.
    """
    if os.environ.get("HARNESS_GUARD_NOLOG") == "1":
        return
    путь = _след_файл()
    try:
        with open(путь, encoding="utf-8") as fh:
            прежняя = json.load(fh)
        os.remove(путь)
    except Exception:
        return
    if time.time() - float(прежняя.get("ts", 0)) > 900:
        return  # четверть часа спустя связь между командами уже выдумана
    было, стало = set(прежняя.get("tokens") or []), _токены(без_тайн(cmd))
    общих = len(было & стало) / max(1, len(было | стало))
    исход = ("повторил почти то же" if общих > 0.6
             else "переиначил" if общих > 0.2 else "отступил")
    try:
        log_dir = read_conf().get("LOG_DIR") or "/var/log/harness"
        with open(os.path.join(log_dir, "guard.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "verdict": "followup",
                "after_rule": прежняя.get("rule", ""),
                "outcome": исход,
                "overlap": round(общих, 2),
                "next_head": без_тайн(cmd.strip()[:120]),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def deny(reason: str) -> int:
    log_deny(reason, ПРОВЕРЯЕМАЯ)
    print(
        f"Команда остановлена хуком.\n\n{reason}\n\n"
        "Сторож отменяет ВСЮ составную команду целиком, даже если охраняемое\n"
        "звено — только её часть. ВАЖНО: если в этом же вызове правился файл\n"
        "(heredoc и т.п.) — правка ТОЖЕ не выполнилась, файла с ней не существует.\n"
        "Не ищи несуществующий дефект: выполни правку отдельным вызовом, а\n"
        "охраняемое действие — своим (или не выполняй вовсе).",
        file=sys.stderr,
    )
    return 2


def check(cmd: str, вложенная: bool = False, предел_снаружи: bool = False) -> int:
    global ПРОВЕРЯЕМАЯ
    if not ПРОВЕРЯЕМАЯ:      # верхний вызов, а не рекурсия по вложенной нагрузке
        log_followup(cmd)
    ПРОВЕРЯЕМАЯ = cmd
    # Правила ищут КОМАНДУ, а не рассказ о ней: тела heredoc и содержимое кавычек
    # гасятся. До 10.09.2026 это делало только правило кириллицы, а позиция
    # команды не пускала отступ — поэтому текст в кавычках почти никогда не
    # совпадал. Как только отступ стал законной позицией (та же дата, дыра
    # «сторож обходится одним пробелом»), незагашенный текст начал давать ложные
    # отмены: строка запрета, приведённая с отступом внутри python-heredoc,
    # отменила запись файла с этой самой правкой — дважды подряд.
    # Команда, спрятанная в кавычках НАРОЧНО (eval с запретом внутри), ловится
    # отдельно — nested_shell_payloads разбирает её как самостоятельную нагрузку.
    видимое = кавычки_погашены(heredoc_вырезан(cmd))
    # bash -c 'claude mcp list' — та же команда в обёртке (В-2). Рекурсия конечна:
    # нагрузка строго короче исходной строки.
    for payload in nested_shell_payloads(cmd):
        # «Снаружи уже стоит timeout N» передаётся внутрь: законная форма
        # «timeout 600 bash -c '<цикл>'» иначе отменялась бы на нагрузке.
        снаружи = bool(ПРЕДЕЛ.search(cmd.split(payload)[0])) if payload in cmd else False
        code = check(payload, вложенная=True, предел_снаружи=снаружи)
        if code:
            return code
    # Рекурсия выше перезаписала след своей нагрузкой — вернуть свою строку,
    # иначе журнал отмен назовёт вложенную команду вместо той, что отменена.
    ПРОВЕРЯЕМАЯ = cmd

    if TELEGRAM_CHANNEL.search(cmd) or TELEGRAM_MCP.search(cmd):
        return deny(
            "Телеграм-канал в этом запуске отберёт getUpdates: Telegram допускает\n"
            "ОДНОГО потребителя на токен, новый держатель убивает живой канал с\n"
            "владельцем (его ведёт демон tg-dispatcher).\n"
            "Остальные MCP-серверы не запрещены — уберите только телеграм."
        )

    if MCP_MUTATE.search(cmd):
        return deny(
            "Правка состава MCP включит сервер НАСОВСЕМ, переживая сессию: так в\n"
            "набор однажды и приезжает телеграм, отбирающий getUpdates у канала.\n"
            "Читать состав можно свободно; менять — задачей с ревью, не на ходу."
        )

    if COMPOSE_RESTART.search(видимое):
        return deny(
            "«docker compose restart» НЕ пересобирает образ — правка не доедет до\n"
            "прода, и проверка покажет старое поведение. Нужно:\n"
            "  docker compose up -d --build <сервис по имени из compose-файла>\n"
            "Деплой целиком — ./scripts/deploy.sh"
        )

    if RMI_PREVIOUS.search(видимое):
        return deny(
            "Удаление образа «:previous» — это снос страховки выката (И-2):\n"
            "именно на него откатывается deploy.sh при мёртвом health, и\n"
            "пересобрать его нечем — прежнего кода в образе уже нет.\n"
            "Снимок обновляется сам при следующем выкате; если снести нужно\n"
            "НАРОЧНО — это необратимо, спроси владельца."
        )

    if PRUNE_ALL.search(видимое):
        return deny(
            "«prune -a» сносит ВСЕ образы без запущенного контейнера — вместе с\n"
            "«:previous», снимком, на который откатывается выкат при мёртвом health\n"
            "(И-2). Образ отката не восстанавливается: его нечем пересобрать.\n"
            "Мусор чистится без -a:\n"
            "  docker image prune -f            # только висячие, previous цел\n"
            "  docker builder prune -f          # кеш сборок\n"
            "Если снести previous нужно НАРОЧНО — это необратимо, спроси владельца."
        )

    # Тела heredoc вырезаны, кавычки — НЕТ. Значение утекает и из кавычек
    # (так и случилось), а внутри heredoc подстановку делает bash при записи
    # в файл — в транскрипт попадает только имя переменной.
    секрет = печать_секрета(heredoc_вырезан(cmd))
    if секрет:
        return deny(
            f"Подстановка «${секрет}» напечатает ЗНАЧЕНИЕ секрета в вывод, а вывод\n"
            "оседает в транскрипте сессии и в логах (улика 12.09.2026: токен на год\n"
            "ушёл в два транскрипта шестью вхождениями и затирался задним числом).\n"
            "Проверять секрет можно, не печатая его:\n"
            f"  echo \"длина: ${{#{секрет}}}\"        # длина\n"
            f"  echo \"${{{секрет}:+выставлена}}\"     # есть или нет\n"
            "Передавать значение — файлом или переменной окружения, не через вывод."
        )

    if DROPDB_CMD.search(cmd) or (DB_CLIENT.search(cmd) and DROP_SQL.search(cmd)):
        return deny(
            "Уничтожение базы (инвариант И-1): drop database / dropdb стирает данные\n"
            "владельца без отката из сессии; восстановление — только из бэкапа и только\n"
            "руками владельца. Если пересоздание базы правда нужно — согласуй в Telegram\n"
            "и дождись явного подтверждения."
        )

    if DB_FILE_FLAG.search(cmd):
        return deny(
            "Клиент БД читает SQL из файла (psql/mysql -f/--file) — SQL из файла\n"
            "сторож прочитать не может, а там может оказаться drop database (И-1).\n"
            "Выполни содержимое явно (например, psql -c '...') или через deploy.sh."
        )

    target = rm_protected_target(cmd)
    if target:
        return deny(
            f"«rm» нацелен на данные/бэкапы/секреты ({target}) — инвариант И-1:\n"
            "данные, бэкапы и секреты из сессии не удаляются — ни каталогом (-r),\n"
            "ни отдельным файлом. Чистить можно временные и сборочные каталоги;\n"
            "судьбу данных решает владелец явно."
        )

    target = find_delete_target(cmd)
    if target:
        return deny(
            f"«find ... -delete» нацелен на каталог данных/бэкапов ({target}) —\n"
            "инвариант И-1: то же уничтожение, что rm -r, только другим глаголом.\n"
            "Судьбу данных решает владелец явно."
        )

    suicide = session_suicide(cmd)
    if suicide and not ротация_ведётся_сторожем():
        return deny(
            f"Сессия завершается мимо ротации ({suicide}). Выход и подъём смены —\n"
            "одно действие, и делает его сторож: выйти самому значит остановить\n"
            "харнес до тех пор, пока владелец не поднимет агента руками\n"
            "(случилось 11.08.2026 в 22:51). Уйти со сменой:\n"
            "  harness/demons/session-warden.sh --rotate-now\n"
            "Он завершит сессию, поднимет преемника и подтвердит приём."
        )

    if DOCKER_CP_IN.search(видимое):
        return deny(
            "«docker cp» внутрь живого контейнера — ручной выкат мимо deploy.sh\n"
            "(инвариант И-2): правка живёт до первой пересборки и не попадает в образ,\n"
            "а рабочее дерево расходится с продом. Правильный путь:\n"
            "  ./scripts/deploy.sh   (сборка → миграции → health)"
        )

    if GIT_PUSH.search(видимое) or DEPLOY_SH.search(видимое):
        # Отправка в репозиторий и выкат спрашиваются РАЗНО: предмет согласия
        # у них свой, и выкат вдобавок перезапускает стек, а push — нет.
        busy = deploy_gate_busy("push" if GIT_PUSH.search(видимое)
                                and not DEPLOY_SH.search(видимое) else "deploy")
        if busy:
            return deny(
                "Ворота деплоя (scripts/deploy_guard.py) ответили «занято» —\n"
                "git push / deploy.sh сейчас нельзя:\n"
                f"  {busy}\n"
                "Дождись освобождения ворот и повтори охраняемое действие."
            )

    имя = имя_кириллицей(кавычки_погашены(heredoc_вырезан(cmd)))
    if имя:
        return deny(
            f"Имя переменной кириллицей: «{имя.group(1)}». Bash такие идентификаторы\n"
            "не берёт и печатает в ошибку САМО ЗНАЧЕНИЕ — 12.08.2026 так утёк токен\n"
            "бота, а 25.08.2026 три замера подряд отдали пустые числа вместо данных.\n"
            "Переименуй латиницей (SP, API, PATH_), кириллицу оставь в тексте и\n"
            "комментариях."
        )

    ban = pack_bans(cmd)
    if ban:
        return deny(ban)

    в_сессии = прогон_в_сессии(cmd)
    if в_сессии:
        return deny(в_сессии)

    # И на внешней команде, и на вложенной нагрузке: «bash -c '<цикл>'» —
    # самая ходовая форма, снаружи её не видно (кавычки погашены), и прежняя
    # редакция пропускала её целиком (ревью кода 12.09.2026).
    # По ВИДИМОМУ, а не по сырой строке: цитата ожидания внутри heredoc —
    # текст документа, а не команда.
    бессрочное = ожидание_без_предела(видимое, предел_снаружи)
    if бессрочное:
        return deny(бессрочное)

    самоловка = ожидание_ловит_себя(cmd)
    if самоловка:
        return deny(самоловка)

    if PYTEST_CALL.search(видимое):
        pids = running_pytest()
        if pids:
            return deny(
                "Прогон тестов уже идёт (процессы: " + ", ".join(pids) + ").\n\n"
                "Тестовая база одна, фикстуры чистят таблицы перед каждым тестом —\n"
                "второй одновременный прогон даёт десятки ложных падений в обоих.\n"
                "Дождись завершения первого."
            )

    return 0


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception as e:
        # Не разобрали вызов — не мешаем работать (fail-open самого сторожа), след — в лог.
        # Длина входа в следе: без неё запись «битый JSON» неотличима — ручной прогон
        # с пустым stdin (улика 11.08.2026, приёмка) или сломавшаяся оболочка, которая
        # шлёт мусор при КАЖДОЙ команде. Первое безобидно, второе значит, что сторожа
        # нет вовсе. Сам вход не пишем: в нём бывают токены, а лог живёт вне репо.
        log_fail_open("битый JSON на stdin", f"{e!r}; на входе байт: {len(raw)}")
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    global СЕССИЯ
    СЕССИЯ = str(payload.get("session_id") or "")
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    try:
        return check(cmd)
    except Exception as e:
        # упал сам сторож — команда проходит (fail-open), но падение видно в логе
        # Через ту же дверь, что и журнал отмен: соседний путь (битый JSON) вход
        # не пишет вовсе — «в нём бывают токены», а здесь оговорка терялась.
        log_fail_open("исключение внутри сторожа", f"{e!r}; команда: {без_тайн(cmd[:200])}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
