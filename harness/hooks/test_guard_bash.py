#!/usr/bin/env python3
# Взят из UNIFIED/templates/hooks/test_guard_bash.py; добавлены пары красный/зелёный на И-1
# (drop database, rm -r по данным), И-2 (docker cp в контейнер) и прогон ворот deploy_guard.py
# через временный конфиг (HARNESS_INSTALL_CONF).
"""Прогон хука-сторожа по всем путям.

Отдельным файлом, а не строкой в оболочке: сторож разбирает текст команды,
и тестовые примеры внутри командной строки он видит как саму команду.

Запускается без /etc/harness/install.conf — это тоже проверка: сторож обязан
работать с дефолтами на свежей машине без конфига: необратимое (push, выкат) без ответа ворот — ОТКАЗ, остальное — fail-open.
"""
import dataclasses
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

# ── носитель стенда: «Да» стенда судья видит только аргументом в процессе пробы ──
# После карточки А (спека §3.2) судья не слушает окружение ни при каком ключе: каталог
# слова, паспорт и окно даёт ему носитель. Стенд подменяет owner_word.носитель в
# процессе, где идёт суд, — в своём (run) или в дочернем процессе-обёртке, который
# импортирует дверь и зовёт её main() (run_вывод, ворота). Свой ключ в окружении
# пробе не нужен: что подменять, обёртке называют доводами.
ОБЁРТКА = "--обёртка-двери"
# Код обёртки, у которой судья без Носитель: ни 0, ни 2 — случай краснеет, а не
# проходит отказом, который дала бы любая поломка.
КОД_ОБЁРТКИ = 3


def _число(значение, умолчание):
    текст = str(значение or "").strip()
    return int(текст) if текст.isdigit() else умолчание


def носитель_стенда(слово_вл, конф, паспорт: str, ручки: str | None):
    """Носитель стенда для судьи `слово_вл`: папка — LOG_DIR паспорта стенда, ручки — его harness.conf.

    Окно и срок кнопки — из ручек стенда, как их читала бы машина из своих. LookupError — у судьи
    нет dataclass Носитель (до А3) или есть обязательное поле, которого проба не знает.
    """
    cls = getattr(слово_вл, "Носитель", None)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise LookupError("в судье нет dataclass Носитель")
    значения = конф.из_файла(паспорт)
    ключи = конф.из_файла(ручки) if ручки else {}
    папка = pathlib.Path(значения.get("LOG_DIR") or паспорт)
    известные = {"папка": папка, "uid": None, "паспорт": значения, "граница": папка,
                 "окно_мин": _число(ключи.get("CONFIRM_MAX_AGE_MIN"), 60),
                 "срок_кнопки_мин": _число(ключи.get("CONSENT_BUTTON_TTL_MIN"), None), "ручки": ключи}
    поля = dataclasses.fields(cls)
    чужие = [п.name for п in поля if п.name not in известные
             and п.default is dataclasses.MISSING and п.default_factory is dataclasses.MISSING]
    if чужие:
        raise LookupError(f"у Носитель обязательные поля, которых проба не знает: {чужие}")
    return cls(**{п.name: известные[п.name] for п in поля if п.name in известные})


def обёртка_двери(доводы: list[str]) -> int:
    """Дочерний процесс: <файл двери> <паспорт стенда> <ручки стенда|-> [доводы двери…].

    Дверь (сторож или ворота) грузится из того же файла, что судит проба (дверь мутаций
    GUARD_UNDER_TEST), её судье подменяется носитель, и зовётся её main() — тем же входом,
    что у харнеса: stdin, доводы, код выхода.
    """
    import importlib.util
    файл, паспорт, ручки, *свои = доводы
    sys.argv = [файл, *свои]
    spec = importlib.util.spec_from_file_location("дверь_под_пробой", файл)
    дверь = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(дверь)
    try:
        носитель = носитель_стенда(дверь.слово_вл, дверь.конф, паспорт, None if ручки == "-" else ручки)
    except LookupError as беда:
        print(f"ОБЁРТКА ПРОБЫ: {беда}", file=sys.stderr)
        return КОД_ОБЁРТКИ
    дверь.слово_вл.носитель = lambda: носитель
    return дверь.main()


def зов_обёртки(файл_двери, env: dict, *доводы: str) -> list[str]:
    """Командная строка обёртки для стенда, чей паспорт и ручки названы в env."""
    return ["python3", str(pathlib.Path(__file__).resolve()), ОБЁРТКА, str(файл_двери),
            env["HARNESS_INSTALL_CONF"], env.get("HARNESS_CONF") or "-", *доводы]


# Обёртка — до побочных действий модуля ниже (свой двор учёта, загрузка сторожа):
# у дочернего процесса их нет, окружение он получил от пробы.
if __name__ == "__main__" and sys.argv[1:2] == [ОБЁРТКА]:
    sys.exit(обёртка_двери(sys.argv[2:]))

# Путь относительно самого теста: набор переносится целиком, тест работает из любого каталога.
# Путь к сторожу подменяем переменной окружения — так мутационная проба
# (scripts/mutation-probe.sh) гоняет боевой тест против ИСПОРЧЕННОЙ копии и
# требует красного. Без подмены пришлось бы запускать «сторож сам себя», и
# красное приходило бы от синтаксической ошибки, а не от пойманной мутации.
GUARD = ["python3", os.environ.get("GUARD_UNDER_TEST")
         or str(pathlib.Path(__file__).resolve().with_name("guard_bash.py"))]
# Настоящие ворота: в пакете лежат в ../scripts/, на сервере — в ../ (scripts/hooks → scripts).
_BASE = pathlib.Path(__file__).resolve().parent
DEPLOY_GUARD = next(
    (p for p in (_BASE.parent / "scripts" / "deploy_guard.py", _BASE.parent / "deploy_guard.py")
     if p.is_file()),
    None,
)

# Правила живого учёта — ДАННЫЕ установки (product-local.conf), и проба подаёт
# СВОИ: иначе случаи ниже судили бы боевые адреса, а имя учётной системы
# заказчика осталось бы в пакете, который уезжает в публичный гит (слово
# владельца 13.10.2026). Литералы стенда нарочно выдуманные.
_ЛИТЕРАЛЫ_УЧЁТА = ("api.erp-obrazec.test", "online.erp-obrazec.test", "erp-token")
_ДВОР_УЧЁТА = tempfile.mkdtemp(prefix="проба-сторожа-учёт-")
_КОНФ_УЧЁТА = os.path.join(_ДВОР_УЧЁТА, "product-local.conf")
with open(_КОНФ_УЧЁТА, "w", encoding="utf-8") as _фх:
    _фх.write('ERP_LIVE_HOSTS="api.erp-obrazec.test online.erp-obrazec.test"\n'
              'ERP_KEY_NAME="erp-token"\n'
              'ERP_STUB_HINT="adapters/erp/test/stub.ts"\n')
os.environ["HARNESS_PRODUCT_CONF"] = _КОНФ_УЧЁТА
# Признак стенда: без него сторож путь из окружения НЕ берёт (иначе переменная
# в чужом профиле снимала бы правила учёта молча).
os.environ["HARNESS_PRODUCT_CONF_STEND"] = "1"

# Базовые случаи: не зависят от конфига, гоняются с окружением по умолчанию.
CASES = [
    # (ожидаемый код, описание, команда)
    (0, "чтение состава MCP — безобидно", "claude mcp list"),
    # Имя переменной кириллицей: bash печатает в ошибку САМО ЗНАЧЕНИЕ.
    # 12.08.2026 так утёк токен бота; 25.08.2026 три замера подряд отдали
    # пустые числа вместо данных.
    (2, "БОЛЬНОЙ СЛУЧАЙ: присваивание кириллической переменной",
     "ПУТЬ=/var/log/harness/лог.txt; cat $ПУТЬ"),
    (2, "тот же случай в цикле: for ИМЯ in", "for ПУТЬ in a b; do echo $ПУТЬ; done"),
    (2, "кириллица после && — команда та же",
     "cd /tmp && ТОКЕН=$(cat /var/lib/harness/secrets/tg_bot_token)"),
    # БОЛЬНОЙ СЛУЧАЙ 09.09.2026: кириллица НЕ первой буквой — прежняя редакция
    # правила пропускала, bash напечатал в ошибку пароль базы.
    (2, "кириллица в СЕРЕДИНЕ имени переменной",
     'DSN_СТЕНД="postgresql://app:пароль@localhost:5435/app"'),
    (0, "латинское имя, кириллица только в значении — законно",
     'OUT_FILE="data/processed/эталон_район.json"'),
    (0, "русский текст в аргументе — не имя переменной",
     "git commit -m \'Правка=1: карта разгружена\'"),
    (0, "русский путь и русское значение при латинском имени",
     "SP=/tmp/скрипты; ls \"$SP\"/замер.sh"),
    (0, "сравнение в условии, а не присваивание", "[ \"$A\" == \"да\" ] && echo ок"),
    # ЛОЖНОЕ СРАБАТЫВАНИЕ первой редакции правила (25.08.2026): python в
    # heredoc — чужой язык, там русские имена законны и приняты в этом проекте.
    (0, "python в heredoc: русские имена внутри — не bash",
     "python3 - <<'PY'\nстрока = 'текст'\nимя = 5\nprint(строка, имя)\nPY"),
    (0, "русское имя в python-heredoc с bash-стилем присваивания",
     "python3 - <<'PY'\nп='/tmp/файл'\nopen(п)\nPY"),
    (2, "команда ПОСЛЕ heredoc проверяется как обычно",
     "python3 - <<'PY'\nx = 1\nPY\nПУТЬ=/tmp/лог; cat $ПУТЬ"),
    (2, "правка состава MCP", "claude mcp add tg npx server"),
    (2, "правка состава MCP без телеграма в имени — состав всё равно меняется", "claude mcp remove gmail"),
    # Путь 16 спеки hr.standards-and-policy: claude из оболочки модели мимо
    # носителя приходит без личности — отказ с готовой командой носителя.
    (2, "headless-запуск мимо носителя — отказ", 'claude -p "сделай X"'),
    (2, "он же в конвейере", 'echo hi | claude -p "x"'),
    (2, "запуск С ТЕЛЕГРАМ-КАНАЛОМ — отберёт getUpdates", 'claude --channels plugin:telegram@claude-plugins-official'),
    (2, "телеграм в наборе MCP", 'claude -p "x" --mcp-config \'{"mcpServers":{"telegram":{"command":"npx"}}}\' --strict-mcp-config'),
    (2, "он же в подстановке", 'out=$(claude -p "x")'),
    (2, "мимо носителя даже со --strict-mcp-config", "claude -p --strict-mcp-config x"),
    (2, "интерактивный claude из оболочки модели", "claude --dangerously-skip-permissions"),
    (0, "через носитель с флагом — разрешён", "bash scripts/claude-run.sh --класс прибор -p --strict-mcp-config x"),
    (0, "носитель абсолютным путём в подстановке",
     'out=$(bash /opt/proekt/scripts/claude-run.sh --класс прибор -p --strict-mcp-config "x")'),
    (0, "claude --version — не модель", "claude --version"),
    (0, "claude --help — не модель", "claude --help"),
    (0, "claude plugin list — не модель", "claude plugin list"),
    (0, "claude auth status — не модель", "claude auth status"),
    # Эталон слоя 1 вне досягаемости модели (спека §5.4).
    (2, "запись в эталон личности /etc/harness/self", "echo 1 > /etc/harness/self/core.json"),
    (2, "tee в эталон личности", "tee /etc/harness/self/instance.yaml < /tmp/x"),
    (2, "подмена печати эталона self-seal", "cp /tmp/x /usr/local/lib/harness/self-seal"),
    (0, "чтение эталона — не запись", "cat /etc/harness/self/core.json"),
    # Н2 ревью ЧП#2: печать сверяет выкладку с манифестом, который пишет сам агент, —
    # вызов печати из команды модели меняет эталон в обход согласия владельца.
    (2, "Н2: модель зовёт печать эталона sudo -n self-seal",
     "sudo -n /usr/local/lib/harness/self-seal /var/www/project"),
    (2, "Н2: печать эталона без sudo", "/usr/local/lib/harness/self-seal /tmp/x"),
    (2, "Н2: печать после другой команды", "cd /tmp && sudo /usr/local/lib/harness/self-seal /tmp/x"),
    (2, "Н2: печать под интерпретатором — скрипт проекта", "bash scripts/self-seal.sh /tmp/x"),
    (2, "Н2: печать по имени self-seal.sh под sudo", "sudo -n /opt/kopiya/scripts/self-seal.sh x"),
    (0, "Н2: чтение файла печати — не вызов", "cat /usr/local/lib/harness/self-seal"),
    (0, "Н2: проба печати — не печать", "bash scripts/test_self_seal.sh"),
    # Н-7 контракта: проба оркестратора живёт в своём tmux-сервере -L.
    (0, "Н-7: kill-server своего tmux-сервера пробы", "tmux -L sap-proba-1 kill-server"),
    (0, "Н-7: носитель внутри своего tmux-сервера пробы",
     'tmux -L sap-proba-1 new-session -d -s proba "bash scripts/claude-run.sh --класс оркестратор --model haiku"'),
    (2, "перезапуск контейнера без пересборки", "docker compose restart backend"),
    # ДЫРА, найденная ревью 10.09.2026: якорь позиции команды не пускал пробелы
    # после начала строки — сторож обходился одним отступом, а внутри if, циклов
    # и функций команды всегда с отступом.
    (2, "команда с отступом — та же команда", "    docker compose restart backend"),
    (2, "команда после then", "if true; then docker compose restart backend; fi"),
    (2, "команда в теле цикла", "for s in api web; do docker compose restart backend; done"),
    # ПРОДОЛЖЕНИЕ ТОЙ ЖЕ ДЫРЫ, замер 10.09.2026: позиция команды знала then/do,
    # но не сами заголовки. Пять форм проходили мимо ВСЕХ правил сторожа.
    (2, "команда в заголовке if", "if docker compose restart api; then :; fi"),
    (2, "команда в заголовке while", "while docker compose restart api; do :; done"),
    (2, "команда в заголовке until", "until docker compose restart api; do :; done"),
    (2, "команда под отрицанием", "! docker compose restart api"),
    (2, "команда под time", "time docker compose restart api"),
    (0, "здоровый цикл чтения не задет", "while read -r line; do echo $line; done"),
    (0, "здоровый time не задет", "time ls -la"),
    # Имя переменной приходит АРГУМЕНТОМ, без «=»: правило присваивания его не
    # видело, и 10.09.2026 `read -r файл …` сорвал мутационную пробу молча.
    (2, "read с кириллическим именем", "while IFS=$'\\t' read -r файл поле; do :; done"),
    (2, "local с кириллическим именем", "local -a слова"),
    # СЕДЬМАЯ форма: имя со ЗНАЧЕНИЕМ после объявления. Правило присваивания
    # требует позиции команды (после `local` её нет), а правило объявления
    # требовало пробела за именем — `local шаг="x"` не ловило НИ ОДНО из двух,
    # и ровно так кириллица прошла в USTANOVIT.sh 13.09.2026 (ревью кода F-12).
    (2, "local с кириллическим именем И значением", 'local шаг="панель"'),
    (2, "export с кириллическим именем и значением", "export ПУТЬ=/tmp/x"),
    (0, "local с латинским именем и значением — законно", 'local step="панель"'),
    (0, "read со списком латинских имён — законно", "read -r src expr proof name"),
    # ШЕСТАЯ форма той же дыры: значение префикса в КАВЫЧКАХ обрывало разбор на
    # пробеле внутри них, и мимо сторожа шла любая запрещённая команда.
    (2, "префикс со значением в кавычках", "LANG='ru RU' docker compose restart api"),
    (2, "префикс $'…' перед запретом", "IFS=$'\\n' docker compose restart api"),
    (2, "он же перед read с кириллицей", "IFS=$'\\t' read -r файл поле"),
    (0, "обычный префикс без кавычек не задет", "LC_ALL=C ls -la"),
    # ВОСЬМАЯ форма обхода, замер 11.09.2026: поиск открытия heredoc шёл по
    # СЫРОЙ строке, поэтому «<<» в кавычках или в арифметике объявляло всё
    # до конца команды телом heredoc — и снимало ВСЕ запреты разом.
    (2, "«<<» внутри кавычек не открывает heredoc",
     'grep -n "<<EOF" scripts/*.sh\ndocker compose restart api'),
    (2, "арифметический сдвиг не открывает heredoc",
     "N=3\nSHIFT=$(( 1 << N ))\ndocker compose restart api"),
    (2, "«<<» в тексте echo не открывает heredoc",
     'echo "a <<TXT b"\ndocker cp /tmp/x api:/app/x'),
    (2, "экранированная кавычка не сбивает разбор",
     'grep -n "kind\\\" == x" f.py && docker compose restart api'),
    (0, "настоящий heredoc: запрет ВНУТРИ тела — текст, а не команда",
     "cat > f.sh <<'SH'\ndocker compose restart api\nSH"),
    (0, "после закрытого heredoc здоровая команда проходит",
     "cat > f.sh <<'SH'\nпривет\nSH\nls -la"),
    (2, "после закрытого heredoc запрет ловится",
     "cat > f.sh <<'SH'\nпривет\nSH\ndocker compose restart api"),
    (0, "строка комментария про запрет — не команда", "# docker compose restart backend"),
    (0, "запрет с отступом ВНУТРИ кавычек — рассказ, а не команда",
     'python3 - <<PY\n    строка = "    docker compose restart backend"\nPY'),
    # БОЛЬНОЙ СЛУЧАЙ 10.09.2026: агент чистил мусор по слову владельца и формой
    # с -a снёс app-api:previous — снимок, на который откатывается выкат.
    # Прод выжил, данные целы, страховки не стало, и вернуть её нечем.
    # БОЛЬНОЙ СЛУЧАЙ 12.09.2026: проверка «дошёл ли долгий токен» напечатала
    # его значение — форма ${VAR:-НЕТ} печатает ЗНАЧЕНИЕ, когда оно есть.
    (2, "печать секрета формой :- ",
     'echo "OAUTH: ${CLAUDE_CODE_OAUTH_TOKEN:+есть}${CLAUDE_CODE_OAUTH_TOKEN:-НЕТ}"'),
    (2, "голая подстановка секрета", "echo $CLAUDE_CODE_OAUTH_TOKEN"),
    (2, "подстановка в скобках", 'printf "%s" "${TG_BOT_TOKEN}"'),
    (2, "пароль тем же правилом", "echo ${POSTGRES_PASSWORD}"),
    # Запись скрипта, который САМ подставит секрет, законна: подстановку делает
    # bash при исполнении, а в транскрипт попадает только имя переменной.
    # Без этого исключения правило отменяло бы правку любого файла, где секрет
    # читается — и первой отменило собственную (живая проба 12.09.2026).
    (0, "секрет внутри heredoc — имя, а не значение", "cat > /tmp/x <<X\n$API_KEY_PROD\nX"),
    (0, "длина значения секрет не выдаёт", 'echo "длина: ${#CLAUDE_CODE_OAUTH_TOKEN}"'),
    (0, "признак есть/нет секрет не выдаёт", 'echo "${CLAUDE_CODE_OAUTH_TOKEN:+выставлена}"'),
    (0, "несекретное имя правилом не трогается", 'echo "$PROJECT_DIR"'),
    (0, "присвоение из файла — не печать", 'export CLAUDE_CODE_OAUTH_TOKEN="$(cat /var/lib/harness/secrets/claude-oauth-token)"'),
    (2, "prune -af сносит образ отката", "docker image prune -af"),
    (2, "prune -a то же длинным флагом", "docker system prune -a --volumes"),
    (2, "флаги в другом порядке — то же правило", "docker image prune -fa"),
    (2, "--all — тот же смысл", "docker image prune --all"),
    (0, "висячие образы без -a: previous цел", "docker image prune -f"),
    (0, "кеш сборок к правилу не относится", "docker builder prune -f"),
    (0, "отбор по возрасту без -a законен", "docker image prune -f --filter until=24h"),
    (0, "рассказ про prune -a — не команда", "echo 'никогда не делай docker image prune -a'"),
    # Ревью 10.09.2026: правило перекрывало один путь из трёх — тот же образ
    # отката сносится прямым rmi и глобальным флагом перед подкомандой.
    (2, "прямое удаление образа отката", "docker rmi app-api:previous"),
    (2, "то же длинной формой", "docker image rm app-web:previous"),
    (2, "глобальный флаг перед подкомандой не спасает", "docker --debug image prune -a"),
    (0, "удаление :latest — не страховка выката", "docker rmi app-api:latest"),
    # Живой учёт заказчика — только чтение (слово владельца 29.09.2026).
    # Больной случай: запись по рабочему адресу стирает карточку, которую ведёт
    # человек, и восстановить её нам нечем — чужая система и чужие копии.
    (2, "PUT в живой учёт",
     "curl -X PUT https://api.erp-obrazec.test/api/remap/1.2/entity/product/7a1 -d '{}'"),
    (2, "DELETE в живой учёт",
     "curl -X DELETE 'https://api.erp-obrazec.test/api/remap/1.2/entity/product/7a1'"),
    (2, "POST длинным флагом",
     "curl --request POST https://online.erp-obrazec.test/api/remap/1.2/entity/product"),
    (2, "-d без метода — это тоже POST",
     "curl -d '{\"name\":\"x\"}' https://api.erp-obrazec.test/api/remap/1.2/entity/product"),
    (2, "метод в теле скрипта",
     "node -e \"fetch('https://api.erp-obrazec.test/api/remap/1.2/entity/product',{method:'PUT'})\""),
    # Слово владельца 29.09 (вечер): «может быть копии достаточно, чтобы
    # обращений в реальную базу не делать». Чтение БЫЛО разрешено — теперь нет:
    # работа идёт по снятой копии, а дверь открывает только его свежее слово.
    (2, "чтение живого учёта тоже запрещено — работаем по копии",
     "curl -s https://api.erp-obrazec.test/api/remap/1.2/entity/product?limit=1"),
    (2, "причина без его слова дверь не открывает",
     "HARNESS_ERP_READ_WHY='сверить поля' curl -s https://api.erp-obrazec.test/api/remap/1.2/entity/product"),
    # ТЕСТОВЫЙ аккаунт учёта — слово владельца 30.09.2026: «делай с ним что
    # хочешь, тестируй как угодно». Признак — имя файла ключа: адрес у обоих
    # аккаунтов один, и хук видит только текст команды.
    (0, "запись на ТЕСТОВЫЙ аккаунт разрешена — его слово 30.09",
     "ERP_TEST_TOKEN_FILE=/var/lib/harness/secrets/erp-token-тестовый.txt "
     "curl -X POST https://api.erp-obrazec.test/api/remap/1.2/entity/product -d '{}'"),
    (0, "чтение тестового аккаунта не требует ни причины, ни его свежего слова",
     "ERP_TEST_TOKEN_FILE=/var/lib/harness/secrets/erp-token-тестовый.txt "
     "curl -s https://api.erp-obrazec.test/api/remap/1.2/entity/product?limit=1"),
    # БОЛЬНОЙ СЛУЧАЙ «не перепутай»: команда называет ОБА ключа. Рабочий тут
    # под рукой, значит это не тестовый аккаунт, и запись отменяется.
    (2, "названы оба ключа — это НЕ тестовый аккаунт",
     "ERP_TOKEN_FILE=/var/lib/harness/secrets/erp-token.txt "
     "ERP_TEST_TOKEN_FILE=/var/lib/harness/secrets/erp-token-тестовый.txt "
     "curl -X PUT https://api.erp-obrazec.test/api/remap/1.2/entity/product/7a1 -d '{}'"),
    # БОЛЬНОЙ СЛУЧАЙ 02.10.2026: ключ подаётся МОНТИРОВАНИЕМ, и переменная
    # называется `ERP_TOKEN_FILE` у обоих аккаунтов. Пока рабочий ключ опознавался
    # по имени переменной, работа с тестовым аккаунтом отменялась наравне с
    # боевой — и законного пути не было вовсе.
    # Адрес учёта в команде назван нарочно: правило УЧЁТ_ЖИВОЙ опознаёт обращение
    # ТОЛЬКО по имени хоста, и без него эти два пути не судятся вовсе (находка
    # 02.10.2026, своя карточка).
    (0, "тестовый ключ подан монтированием через ERP_TOKEN_FILE — это тестовый аккаунт",
     "docker compose --profile test run --rm "
     "-v /var/lib/harness/secrets/erp-token-тестовый.txt:/ключ:ro "
     "-e ERP_TOKEN_FILE=/ключ -e ERP_URL=https://api.erp-obrazec.test/api/remap/1.2 "
     "test node scripts/load-stock.ts"),
    (2, "боевой ключ монтированием через ERP_TOKEN_FILE — дверь закрыта",
     "docker compose --profile test run --rm "
     "-v /var/lib/harness/secrets/erp-token.txt:/ключ:ro "
     "-e ERP_TOKEN_FILE=/ключ -e ERP_URL=https://api.erp-obrazec.test/api/remap/1.2 "
     "test node scripts/load-stock.ts"),
    (2, "имя тестового файла НЕ читается как рабочий ключ, но и наоборот тоже",
     "ERP_TOKEN_FILE=/var/lib/harness/secrets/erp-token.txt SYNC_PUSH_ENABLED=1 node worker.js"),
    (0, "выгрузка с ТЕСТОВЫМ ключом — законна",
     "ERP_TEST_TOKEN_FILE=/var/lib/harness/secrets/erp-token-тестовый.txt SYNC_PUSH_ENABLED=1 node worker.js"),
    (0, "копия и записанные ответы — не живой учёт",
     "node scripts/make-recorded-fixtures.ts"),
    (0, "запись на заглушку — не живой учёт",
     "curl -X PUT http://localhost:18191/entity/product/7a1 -d '{}'"),
    (0, "рассказ про запись — не команда",
     "echo 'в api.erp-obrazec.test ничего не пишем -X PUT'"),
    (2, "включение выгрузки рядом с живым ключом",
     "ERP_TOKEN_FILE=/var/lib/harness/secrets/erp-token.txt SYNC_PUSH_ENABLED=1 node worker.js"),
    # Снятие ответов ходит в живой учёт своим адресом внутри скрипта, но в
    # КОМАНДЕ адреса нет — правило его не видит и видеть не может: хук судит
    # команду. Носитель этого пути — не хук, а сам скрипт (выгрузка выключена
    # наглухо) и слово владельца на запуск.
    (0, "запуск снятия ответов: адреса в команде нет, судит не хук",
     "ERP_TOKEN_FILE=/var/lib/harness/secrets/erp-token.txt node scripts/record-erp-responses.ts"),
    (2, "пустой набор MCP мимо носителя — отказ", "claude -p \"x\" --mcp-config '{\"mcpServers\":{}}' --strict-mcp-config"),
    (0, "запуск с пустым набором MCP через носитель",
     "bash scripts/claude-run.sh --класс прибор -p \"x\" --mcp-config '{\"mcpServers\":{}}' --strict-mcp-config"),
    (0, "пересборка", "docker compose up -d --build backend"),
    (0, "обычная команда", "git status --short"),
    (0, "ТЕКСТ про запрет, а не команда", 'echo "никогда не запускай claude mcp add — оборвётся связь"'),
    (0, "текст в середине строки", 'grep -n "claude mcp" docs/*.md'),
    # Обратная кавычка размечает код в markdown. Пока она считалась позицией
    # команды, сторож не давал писать документацию про самого себя.
    (0, "разметка кода в markdown", "echo 'смотри `claude mcp list` в документации'"),
    (2, "команда после разделителя &&", "cd /tmp && claude mcp add x y"),
    (2, "команда после точки с запятой", "cd /tmp; claude mcp add x y"),
    # --- И-1: уничтожение базы ---
    (2, "И-1: drop database через psql", 'psql -U app -h 127.0.0.1 -c "DROP DATABASE appdb"'),
    (2, "И-1: dropdb", "dropdb appdb"),
    (2, "И-1: drop database внутри docker compose exec", 'docker compose exec -T db psql -U app -c "drop database appdb"'),
    (0, "текст про drop database — не команда", 'echo "никогда не делай drop database руками"'),
    (0, "psql с безобидным запросом", 'psql -U app -c "select count(*) from users"'),
    (0, "grep по слову dropdb — не команда", 'grep -rn "dropdb" docs/'),
    (0, "grep -E с «|» перед dropdb — шаблон, не конвейер", "grep -E 'psql|dropdb' docs/x.md"),
    (0, "rg с «|» перед dropdb — шаблон", "rg -n 'a|dropdb' ."),
    (2, "И-1: dropdb после конвейера", "ls | xargs dropdb"),
    (2, "И-1: dropdb в sh -c", "sh -c 'dropdb appdb'"),
    (2, "И-1: dropdb в теле heredoc для bash", "bash <<'EOF'\ndropdb appdb\nEOF"),
    (2, "И-1: dropdb в теле heredoc для sh", "sh <<EOF\necho a\ndropdb appdb\nEOF"),
    (2, "И-1: dropdb в heredoc, поданном в конвейер на bash", "cat <<EOF | bash\ndropdb appdb\nEOF"),
    (0, "dropdb в тексте heredoc для cat — не команда", "cat <<EOF\ndropdb appdb\nEOF"),
    # --- В-2-остаток: SQL из файла сторожу не виден — узкий fail-closed ---
    (2, "В-2: psql -f — SQL в файле, сторож его не прочитает", "psql -U app -f drop.sql"),
    (2, "В-2: psql --file — то же", "psql --file=migrate.sql appdb"),
    (2, "В-2: mysql -f по конвейеру не спрятать", "cd /tmp && mysql -u root -f dump.sql"),
    (0, "В-2: psql -c с явным SQL — проходит", "psql -c 'select 1'"),
    (0, "В-2: текст про psql -f — не команда", 'echo "psql -f drop.sql руками не гонять"'),
    # --- И-1: rm по данным/бэкапам ---
    (2, "И-1: rm -rf по каталогу данных", "rm -rf /opt/app/data"),
    (2, "И-1: rm -rf по бэкапам", "rm -rf /var/backups/app"),
    (2, "И-1: rm -rf по данным в составной команде — отмена целиком", "mkdir -p /tmp/x && rm -rf ./data/uploads"),
    (0, "rm -rf по временному каталогу", "rm -rf /tmp/build-cache"),
    # Приёмка 11.08.2026, живой случай: «rm -f» по файлу внутри SECRETS_DIR
    # прошёл мимо сторожа — правило требовало рекурсии, а И-1 требует не терять
    # данные. Теперь рекурсия не нужна: цена — отказ на «rm data/stale.lock»
    # (снять лок можно из каталога временных или спросив владельца).
    (2, "И-1: rm -f файла с секретом (без рекурсии)", "rm -f /var/lib/harness/secrets/tg_bot_token"),
    (2, "И-1: rm одиночного файла бэкапа", "rm /var/backups/harness/2026-08-11/project.bundle"),
    (2, "И-1: rm без рекурсии внутри каталога данных", "rm -f data/stale.lock"),
    (0, "rm без рекурсии по временному файлу — проходит", "rm -f /tmp/сборка.log"),
    (0, "текст про rm -rf — не команда", 'echo "rm -rf /data делать нельзя"'),
    # --- П-10: цели rm ищутся в операторе с самим rm, комментарий — не цель ---
    (0, "П-10: data/ в комментарии — не цель rm", "cat список.txt | xargs rm -rf  # чистка data/x"),
    (2, "П-10: data слева от конвейера ловится и с комментарием", "find /opt/app/data -mtime +1 | xargs rm -rf  # уборка"),
    (0, "П-10: data в соседней команде за ; — не цель rm", "ls /opt/app/data; cat список.txt | xargs rm -rf"),
    # --- И-2: ручной выкат мимо deploy.sh ---
    (2, "И-2: docker cp внутрь контейнера", "docker cp backend/app/main.py backend:/app/main.py"),
    (2, "И-2: docker cp внутрь в составной команде — отмена целиком", "git stash && docker cp app.py backend:/app/app.py"),
    (0, "docker cp ИЗ контейнера наружу (логи) — можно", "docker cp backend:/app/log.txt /tmp/log.txt"),
    (0, "текст про docker cp — не команда", 'echo "docker cp app backend:/app — ручной выкат, нельзя"'),
    # --- Уход из сессии без смены: больной случай 11.08.2026, 22:51 ---
    # Ровно та команда, которой сессия завершила себя и не оставила преемника.
    (2, "БОЛЬНОЙ СЛУЧАЙ: /exit в панель по пустой переменной — цель неизвестна, значит своя",
     '''S=$(grep -h "^TMUX_SESSION=" /etc/harness/install.conf | cut -d'"' -f2); tmux send-keys -t "$S" -l -- "/exit"'''),
    (2, "/exit в панель без -t — это текущая, то есть своя", 'tmux send-keys -l -- "/exit"'),
    (2, "kill-session по сессии агента", "tmux kill-session -t agent"),
    (2, "kill-server закрывает всё, включая панель агента", "tmux kill-server"),
    (2, "снять агента сигналом — тот же уход без смены", "pkill -f claude"),
    (0, "kill-session ЧУЖОЙ одноразовой сессии (селфтесты сторожа) — можно",
     "tmux kill-session -t warden-selftest-3-42"),
    (0, "пинок текстом в панель — доставка, а не выход", 'tmux send-keys -t agent -l -- "продолжай задачу"'),
    (0, "законный уход со сменой — через сторожа", "harness/demons/session-warden.sh --rotate-now"),
    (0, "текст про выход — не команда", 'echo "tmux kill-session -t agent — так нельзя"'),
    # --- 12.08.2026: две дыры, найденные пробой «tmux слетел целиком» ---
    # Первая: уход можно было отдать чужим рукам. Отложенный запуск уводит
    # команду из позиции команды, и правило её не видело — а сессия умирает
    # ровно так же, только через минуту.
    (2, "БОЛЬНОЙ СЛУЧАЙ: уход отложенным запуском systemd-run",
     "systemd-run --on-active=45 --unit=проба /usr/bin/tmux kill-server"),
    (2, "тот же обход с коротким флагом при аргументе",
     "systemd-run -u проба --on-active=30 tmux kill-server"),
    (2, "setsid прячет уход так же", "setsid tmux kill-session -t agent"),
    (0, "systemd-run с безобидной командой — не мешаем",
     "systemd-run --on-active=60 /usr/bin/true"),
    # Вторая, обратная: сторож считал своим ЛЮБОЙ tmux. Панель агента живёт на
    # сокете по умолчанию; проба на своём сокете её не касается, а запрет на
    # неё запрещал ровно ту проверку аварии, ради которой сторож и написан.
    (0, "БОЛЬНОЙ СЛУЧАЙ: kill-server чужого tmux-сервера (-L) — панели агента там нет",
     "tmux -L проба kill-server"),
    (0, "то же через -S: сокет свой, отдельный", "tmux -S /tmp/проба.sock kill-server"),
    (0, "отложенный запуск по чужому серверу — обе правки не спорят",
     "systemd-run --on-active=45 tmux -L проба kill-server"),
    (2, "-L с подстановкой: сокет неизвестен, значит свой", 'tmux -L "$SOCK" kill-server'),
    (2, "-L default — это и есть сервер агента", "tmux -L default kill-server"),
    (2, "-S на путь сокета по умолчанию — тоже свой", "tmux -S /tmp/tmux-1000/default kill-server"),
    # --- В-2: обходы из ревью 09.08.2026 — на каждый пара «обход/безопасный аналог» ---
    (2, "В-2: env-префикс перед rm по данным", "LC_ALL=C rm -rf /opt/app/data"),
    (0, "В-2: env-префикс перед rm по временным", "LC_ALL=C rm -rf /tmp/build-cache"),
    (2, "В-2: timeout перед claude мимо носителя", "timeout 60 claude -p x"),
    (0, "В-2: timeout перед носителем", "timeout 60 bash scripts/claude-run.sh --класс прибор -p --strict-mcp-config x"),
    (2, "В-2: timeout перед claude с телеграм-каналом", "timeout 60 claude --channels plugin:telegram@x"),
    (0, "В-2: timeout перед безобидной командой", "timeout 60 curl -s http://127.0.0.1:8010/health"),
    (2, "В-2: nohup перед правкой состава MCP", "nohup claude mcp add x y"),
    (0, "В-2: nohup перед безобидной командой", "nohup sleep 5"),
    (2, "В-2: xargs rm -rf, цели по stdin из каталога данных", "find /opt/app/data -name '*.bak' | xargs rm -rf"),
    (0, "В-2: xargs rm -rf по временным", "find /tmp/build -name '*.tmp' | xargs rm -rf"),
    (2, "В-2: bash -c с правкой состава MCP внутри", "bash -c 'claude mcp add x y'"),
    (0, "В-2: bash -c с безобидной командой", "bash -c 'ls /tmp'"),
    (2, "В-2: sh -c с телеграм-каналом внутри", 'sh -c "claude --channels plugin:telegram@x"'),
    (2, "В-2: docker-compose (v1) restart", "docker-compose restart backend"),
    (0, "В-2: docker-compose (v1) up --build", "docker-compose up -d --build backend"),
    (2, "В-2: compose restart с -f между", "docker compose -f docker-compose.yml restart backend"),
    (0, "В-2: compose up --build с -f между", "docker compose -f docker-compose.yml up -d --build backend"),
    (2, "В-2: полный путь /usr/bin/dropdb", "/usr/bin/dropdb appdb"),
    (0, "В-2: полный путь безобидной команды", "/usr/bin/psql -U app -c 'select 1'"),
    (2, "В-2: /usr/local/bin/dropdb", "/usr/local/bin/dropdb appdb"),
    (2, "В-2: find -delete по каталогу данных", "find /opt/app/data -type f -delete"),
    (0, "В-2: find -delete по временным", "find /tmp/cache -type f -delete"),
    (0, "В-2: find по данным БЕЗ -delete", "find /opt/app/data -name '*.py' -print"),
    (2, "В-2: docker cp контейнер→контейнер", "docker cp backend:/app/x.py worker:/app/x.py"),
    (0, "В-2: docker cp контейнер→наружу (дамп)", "docker cp backend:/app/dump.sql /tmp/dump.sql"),
    # --- В-3: ложные срабатывания — паттерны заякорены на позицию команды ---
    (0, "В-3: echo про compose restart — не команда", "echo 'не делай docker compose restart'"),
    (0, "В-3: grep по документации про compose restart", "grep -rn 'docker compose restart' docs/"),
    (0, "В-3: grep по слову pytest — не прогон", "grep -rn pytest docs/"),
    # --- прогон только фоном (указание владельца 23.08.2026) ---
    (2, "make smoke в сессии", "cd app && make smoke"),
    (2, "make прогон-быстрый в сессии", "make прогон-быстрый"),
    (2, "make smoke-тест в сессии", "make smoke-тест"),
    (2, "прогон сезона движка в сессии", ".venv/bin/python collectors/progon_dvizhka.py --от 2025-09-15 --до 2026-04-15 --записать"),
    (0, "имя скрипта прогона В ТЕКСТЕ — не запуск", "echo 'улика: collectors/progon_dvizhka.py шёл 18 минут'"),
    (0, "make smoke фоном — разрешено", "setsid nohup make smoke > /var/log/harness/smoke.log 2>&1 < /dev/null &"),
    (0, "поднять тестовый стенд — не прогон", "make тест-стенд"),
    (0, "залить данные в тестовый стенд — не прогон", "make тест-стенд-данные"),
    (0, "В-3: echo про pytest — не прогон", "echo 'запусти pytest позже'"),
    # --- eval: нагрузка разбирается как команда, наравне с bash -c ----------
    # БОЛЬНОЙ СЛУЧАЙ 09.09.2026, найден замером: `bash -c "..."` сторож
    # разбирал, а `eval "..."` — нет, и запрет И-2 обходился одним словом.
    (2, "БОЛЬНОЙ СЛУЧАЙ: запрет И-2 внутри eval",
     'eval "docker compose restart backend"'),
    (2, "eval в одинарных кавычках — то же", "eval 'docker compose restart backend'"),
    (2, "кириллическое имя внутри eval", 'eval "СЕТЬ=1"'),
    (2, "drop database внутри eval", 'eval "dropdb appdb"'),
    (0, "eval безобидной команды", 'eval "git status --short"'),
    # --- кавычки: внутри них не bash, а текст и чужие языки -----------------
    (0, "питон в кавычках: русское имя переменной — не bash",
     "python3 -c 'for г in (1,2): print(г)'"),
    # Пережито 09.09.2026: в МНОГОСТРОЧНОМ питоне `for` встаёт в начало строки,
    # и это выглядит как позиция команды bash.
    (0, "многострочный питон в кавычках — цикл в начале строки",
     "python3 -c '\nимена = []\nfor г in имена:\n    print(г)\n'"),
    (0, "текст сообщения с присваиванием внутри", 'say "сводка (гейты=$N скилы=$S)"'),
    (0, "то же в одинарных кавычках", "echo 'прибор молчит (работа=$W)'"),
    # --- ожидание, ловящее само себя (снимок владельца 09.09.2026) ---
    # БОЛЬНОЙ СЛУЧАЙ: шаблон стоит в командной строке самого ожидающего
    # процесса, pgrep находит его и ждёт вечно. Замер 09.09.2026: ожидание по
    # заведомо несуществующему имени висело по таймауту (код 124).
    (2, "БОЛЬНОЙ СЛУЧАЙ: ожидание ждёт само себя",
     'until ! pgrep -f "collectors/progon_dvizhka.py" >/dev/null; do sleep 30; done'),
    (2, "то же ожидание циклом while", 'while pgrep -f "pytest -q" >/dev/null; do sleep 10; done'),
    (2, "разовая проверка живости врёт так же — находит свою оболочку",
     'pgrep -af "collectors/progon_dvizhka.py"'),
    (2, "шаблон без кавычек — самосовпадение то же", "pgrep -f progon_dvizhka.py"),
    (2, "pkill -f по шаблону, который убьёт и саму команду", 'pkill -f "progon_dvizhka.py"'),
    (0, "класс символов ломает самосовпадение — законная форма",
     'until ! pgrep -f "[c]ollectors/progon_dvizhka.py" >/dev/null; do sleep 30; done'),
    (0, "ожидание по PID — рекомендованная форма", "until ! kill -0 243581 2>/dev/null; do sleep 30; done"),
    # п.4 отчёта чужого сервера 25.10: разовая проверка живости той формой, которую
    # сторож советует сам, обязана проходить в любой записи кавычек.
    (0, "п.4: рекомендованная форма «[t]g-…» в двойных кавычках проходит",
     'pgrep -af "[t]g-dispatcher"'),
    (0, "п.4: та же форма в одинарных кавычках", "pgrep -af '[t]g-dispatcher'"),
    (0, "п.4: та же форма без кавычек", "pgrep -af [t]g-dispatcher"),
    (0, "п.4: класс с продолжением имени (.sh)", 'pgrep -af "[t]g-dispatcher.sh"'),
    (2, "п.4: голое имя tg-dispatcher — самосовпадение, отказ", 'pgrep -f "tg-dispatcher"'),
    (2, "п.4: голое имя через -af", "pgrep -af tg-dispatcher"),
    # --- ожидание без предела времени (замер 12.09.2026) ---
    # БОЛЬНОЙ СЛУЧАЙ, ради которого правило: ожидание строки в логе не кончится
    # НИКОГДА, если прогон упал и строки не будет. Замер по журналам: 96 раз
    # сессия не закрывалась на вопросе о фоновых задачах, в списке каждый раз
    # десять таких ожиданий — выход не проходил, и дальше шла вся цепочка
    # (тревога, перезапуск, выпадение в оболочку, простой).
    (2, "БОЛЬНОЙ СЛУЧАЙ: ожидание строки в логе без предела времени",
     'until grep -qE "passed|failed" /var/log/harness/x.log; do sleep 30; done'),
    (2, "то же циклом while — форма записи не меняет беды",
     'while ! grep -q "готово" /var/log/harness/x.log; do sleep 10; done'),
    (0, "то же ожидание с пределом — законная форма",
     'timeout 600 bash -c \'until grep -qE "passed|failed" /var/log/harness/x.log; do sleep 30; done\''),
    (0, "ожидание по PID предел не требует: процесс кончится и цикл сам выйдет",
     "until ! kill -0 243581 2>/dev/null; do sleep 30; done"),
    (0, "цикл чтения без sleep — не ожидание",
     'while IFS= read -r line; do echo "$line"; done < spisok.txt'),
    # БОЛЬНОЙ СЛУЧАЙ ложной отмены: цитата ожидания внутри heredoc — это
    # ТЕКСТ документа. 12.09.2026 правило отменило запись передачи смены,
    # в которой разбирался этот самый случай.
    # Дыры правила, найденные ревью кода 12.09.2026: форма записи не должна
    # решать, сторожит правило или нет.
    (2, "БОЛЬНОЙ: то же ожидание в обёртке bash -c — самая ходовая форма",
     'bash -c \'until grep -q x /var/log/harness/x.log; do sleep 30; done\''),
    (2, "БОЛЬНОЙ: то же под setsid — обёртка глубже",
     'setsid bash -c \'until grep -q x /var/log/harness/x.log; do sleep 30; done\''),
    (2, "БОЛЬНОЙ: timeout в СОСЕДНЕЙ команде предела циклу не даёт",
     'timeout 5 echo привет; until grep -q x /var/log/harness/x.log; do sleep 30; done'),
    (0, "цикл чтения файла с паузой — не ожидание: вход конечен",
     'while read -r line; do echo "$line"; sleep 1; done < spisok.txt'),
    (0, "цитата ожидания внутри heredoc — текст, а не команда",
     "cat >> docs/handover.md <<'HAND'\n"
     "Ждали так: until grep -q x log; do sleep 30; done\n"
     "HAND"),
    (0, "pgrep без -f смотрит имя процесса, а не командную строку", "pgrep pytest"),
    (0, "шаблон из переменной — раскрытия нет, самосовпадения не доказать", 'pgrep -f "$ИМЯ"'),
    (0, "В-3: pgrep в тексте документации — не команда", "grep -rn 'pgrep -f' docs/"),
    (0, "В-3: echo про pgrep — не команда", "echo 'не жди через pgrep -f progon_dvizhka.py'"),
    # Тревогу заводит и снимает сторож, а не смена (R1-01, второй носитель после
    # родословной процесса): отрыв через setsid делает сироту без предка claude.
    (2, "ALERT-1: alert-task.py --resolve из сессии",
     "python3 scripts/alert-task.py --resolve nightly-gates:red"),
    (2, "ALERT-2: --key из сессии с пустым CLAUDE_CODE_SESSION_ID=",
     "CLAUDE_CODE_SESSION_ID= python3 scripts/alert-task.py --key a:b --title t"),
    (2, "ALERT-3: env -u снимает признак сессии — правило всё равно ловит",
     "env -u CLAUDE_CODE_SESSION_ID python3 scripts/alert-task.py --resolve a:b"),
    (2, "ALERT-4: отрыв setsid nohup с полным путём",
     "setsid nohup python3 /var/www/project/scripts/alert-task.py --resolve a:b > /dev/null 2>&1 &"),
    (2, "ALERT-5: timeout перед писателем",
     "timeout 30 python3 scripts/alert-task.py --key a:b --title t"),
    (0, "ALERT-6: --is-green — чтение, не запрещено",
     "python3 scripts/alert-task.py --is-green nightly-gates:red"),
    (0, "ALERT-7: --open — чтение, не запрещено", "python3 scripts/alert-task.py --open"),
    (0, "ALERT-8: имя писателя в grep — не зов", "grep -n -- '--resolve' scripts/alert-task.py"),
]

# Литералы стенда учёта стоят в случаях ТЕКСТОМ (подстановка внутри строки
# случая не раскрылась бы молча), поэтому сходство со стендом проверяется
# здесь: разойдись они — случаи учёта судили бы команду БЕЗ адреса учёта, то
# есть были бы зелёными без работы.
_КОМАНДЫ_СЛУЧАЕВ = " ".join(к for _, _, к in CASES)
for _литерал in _ЛИТЕРАЛЫ_УЧЁТА:
    assert _литерал in _КОМАНДЫ_СЛУЧАЕВ, (
        f"стенд учёта знает «{_литерал}», а ни один случай его не называет")

# Гнездо tmux: каталог /tmp/tmux-<uid> держит сокет ЖИВОГО сервера агента; снесённое
# гнездо сторож смены читает как «агент пропал» и поднимает вторую смену (улики
# 29.10.2026). Решается по тексту команды, ничего не удаляется.
CASES += [
    (2, "гнездо tmux: чистка /tmp всего, кроме claude-* (улика 1)",
     "find /tmp -mindepth 1 -maxdepth 1 ! -name 'claude-*' -exec rm -rf {} +"),
    (2, "гнездо tmux: find -name 'tmux-*' -delete (улика 3)",
     "find /tmp -name 'tmux-*' -delete"),
    (2, "гнездо tmux: rm -rf /tmp/tmux-* (шаблон не решается)", "rm -rf /tmp/tmux-*"),
    (2, "гнездо tmux: rm -rf /tmp/* сносит всё", "rm -rf /tmp/*"),
    (2, "гнездо tmux: rm -rf /tmp целиком", "rm -rf /tmp"),
    (2, "гнездо tmux: find /tmp -exec rm без исключения", "find /tmp -mindepth 1 -exec rm -rf {} +"),
    (2, "гнездо tmux: find /tmp -mtime -delete без выборки по имени", "find /tmp -mtime +1 -delete"),
    (0, "гнездо tmux: свой каталог в /tmp/claude-*", "rm -rf /tmp/claude-1004/scratch/x"),
    (0, "гнездо tmux: свой временный файл", "rm -f /tmp/my-probe.log"),
    (0, "гнездо tmux: find -name '*.log' -delete", "find /tmp -name '*.log' -delete"),
    (0, "гнездо tmux: чистка с исключением tmux-* и claude-*",
     "find /tmp -mindepth 1 -maxdepth 1 ! -name 'claude-*' ! -name 'tmux-*' -exec rm -rf {} +"),
    (0, "гнездо tmux: только обычные файлы (-type f)", "find /tmp -type f -mtime +3 -delete"),
    (0, "дверь (а): чистка /tmp штатным tmp-sweep", "python3 scripts/tmp-sweep.py"),
    (0, "дверь (б): свой пробный сервер гасится через tmux", "tmux -L w2probe kill-server"),
    # Ревью M2: тот же снос другой записью пути или другим глаголом.
    (2, "гнездо tmux: find | xargs rm без исключения", "find /tmp -name 'tmux-*' | xargs rm -rf"),
    (2, "гнездо tmux: cd /tmp и шаблон относительно", "cd /tmp && rm -rf tmux-*"),
    (2, "гнездо tmux: cd /tmp и find . -delete", "cd /tmp && find . -delete"),
    (2, "гнездо tmux: cd /tmp и find . -path './tmux-*'", "cd /tmp && find . -path './tmux-*' -delete"),
    (2, "гнездо tmux: find / -path '/tmp/tmux-*'", "find / -path '/tmp/tmux-*' -delete"),
    (2, "гнездо tmux: find / -mtime без выборки по имени", "find / -mtime +1 -delete"),
    (2, "гнездо tmux: //tmp — тот же /tmp", "rm -rf //tmp"),
    (2, "гнездо tmux: /var/../tmp — тот же /tmp", "rm -rf /var/../tmp"),
    (2, "гнездо tmux: mv шаблоном уносит гнездо", "mv /tmp/tmux-* /tmp/old"),
    (0, "гнездо tmux: mv В /tmp — цель не сносится", "mv build.log /tmp/"),
    (0, "гнездо tmux: cd /tmp и свой файл", "cd /tmp && rm -f my-probe.log"),
    (0, "гнездо tmux: cd в чужой каталог и tmux-* там", "cd src && rm -rf tmux-1004"),
    (0, "гнездо tmux: find / -name '*.pyc' -delete", "find / -name '*.pyc' -delete"),
    (0, "гнездо tmux: unlink своего сокета", "unlink /tmp/my.sock"),
]

# Случаи БЕЗ install.conf: (ожидаемый код, описание, команда). Гоняются с
# HARNESS_INSTALL_CONF на заведомо несуществующий файл. Раньше лежали в CASES,
# где окружение наследуется целиком, — и на сервере с установленным
# /etc/harness/install.conf «без конфига» означало «с боевым конфигом»:
# ворота честно отвечали «занято» (semi без свежего согласия), тест краснел.
# Улика приёмки 11.08.2026: проверка обязана СОЗДАВАТЬ условие, а не надеяться
# на состояние машины.
NOCONF_CASES = [
    (2, "без install.conf ворота не отвечают: git push — ОТКАЗ (fail-closed)", "git push"),
    (2, "без install.conf ворота не отвечают: deploy.sh — ОТКАЗ (fail-closed)", "./scripts/deploy.sh"),
]

# Случаи с воротами: (ожидаемый код, описание, команда, ворота_заняты)
GATE_CASES = [
    (2, "ворота заняты: git push отменён", "git push", True),
    (2, "ворота заняты: deploy.sh отменён", "./scripts/deploy.sh", True),
    (2, "ворота заняты: bash scripts/deploy.sh отменён", "bash scripts/deploy.sh", True),
    (2, "ворота заняты: push в составной команде — отмена целиком", 'git add -A && git commit -m "x" && git push origin main', True),
    (2, "В-2: ворота заняты: git -C /path push тоже отменён", "git -C /opt/app push origin main", True),
    (0, "В-2: git -C /path status — не push, проходит", "git -C /opt/app status", True),
    (0, "ворота заняты, но команда не push/deploy", "git status", True),
    (0, "ворота заняты: текст про git push — не команда", 'echo "не забудь git push"', True),
    (0, "ворота свободны: git push проходит", "git push", False),
    (0, "ворота свободны: deploy.sh проходит", "./scripts/deploy.sh", False),
    (0, "ворота заняты: deploy.sh --план ничего не выкатывает — проходит", "./scripts/deploy.sh --план", True),
    (0, "ворота заняты: deploy.sh --selftest-dump — самотест, проходит", "bash scripts/deploy.sh --selftest-dump", True),
    (2, "ворота заняты: --план и выкат в одной строке — выкат отменён", "./scripts/deploy.sh --план && ./scripts/deploy.sh", True),
    (2, "ворота заняты: --планы — не ключ плана, отменён", "./scripts/deploy.sh --планы", True),
]

FAKE_GUARD_BUSY = (
    "import sys\n"
    'print("занято: идёт выкат, начатый другой сессией", file=sys.stderr)\n'
    "sys.exit(1)\n"
)
FAKE_GUARD_FREE = "import sys\nsys.exit(0)\n"



ПРИЧИНА_В_СЕССИИ = "Внутри сессии запускается"

# (ожидаем ли отказ ИМЕННО за запуск в сессии, описание, команда)
ФОНОВЫЕ_СЛУЧАИ = [
    (True, "прогон набора в сессии", "docker compose exec -T backend python -m pytest tests/ -q"),
    (True, "прогон из venv по каталогу набора", ".venv/bin/python -m pytest -q tests/"),
    (True, "выборка по -k, без файлов — тот же полный проход", ".venv/bin/python -m pytest -q -k карта"),
    (False, "тот же прогон фоном", "setsid nohup docker compose exec -T backend python -m pytest tests/ -q > /var/log/harness/x.log 2>&1 < /dev/null &"),
    (False, "соло-прогон упавшего теста", ".venv/bin/python -m pytest -q tests/test_obem_ui.py::test_наклон"),
    (False, "два файла набора — разбор, не прогон", "python -m pytest -q tests/test_a.py tests/test_b.py"),
    (False, "соло-прогон ПОСЛЕ && — цель видна за разделителем",
     "cd /var/www/x && API_URL=http://localhost:3001 .venv/bin/python -m pytest -q tests/test_map_ui.py::test_клик --tb=short"),
    (False, "сборка тестов (--collect-only)", "python -m pytest -q --collect-only tests/ > /dev/null"),
    (False, "путь прогона в разметке markdown — текст, не команда",
     "python3 - <<PY\nтекст = '(`.venv/bin/python -m pytest` сторож не видел)'\nPY"),
]


def проверить_дверь_dannyh() -> tuple[int, int]:
    """Пара красный/зелёный на честную дверь удаления ДАННЫХ.

    Случай 15.10.2026: владелец перенёс продукт на другой сервер и велел
    вычистить прежний, а правило о данных отменяло всякое `rm` в секретах и
    копиях, не предлагая ничего взамен («судьбу данных решает владелец явно» —
    а чем её исполнить, не сказано). Дверь открывают ДВА условия разом, и
    проба судит каждое по отдельности.
    """
    bad = 0
    путей = 0
    цель = "/var/www/проба-данных/secrets"

    def строка_журнала(текст: str, message_id: str | None = "552") -> str:
        когда = datetime.datetime.now().astimezone()
        запись = {"ts": когда.isoformat(), "chat_id": "1000000001", "текст": текст}
        if message_id is not None:
            запись["message_id"] = message_id
        return json.dumps(запись, ensure_ascii=False) + "\n"

    случаи = [
        (2, "без причины отказ как прежде", f"rm -rf {цель}"),
        (2, "пустая причина дверь не открывает",
         f"HARNESS_DATA_DELETE_WHY='' rm -rf {цель}"),
        (2, "причина из одних пробелов — не слово",
         f"HARNESS_DATA_DELETE_WHY='   ' rm -rf {цель}"),
    ]
    for ждём, имя, cmd in случаи:
        путей += 1
        код = run(cmd)
        if код != ждём:
            print(f"  ПЛОХО {имя}: ждали {ждём}, вышло {код}")
            bad += 1
        else:
            print(f"  ок    {имя}")

    with tempfile.TemporaryDirectory() as двор:
        команда = (f"HARNESS_DATA_DELETE_WHY='его слово 15.10' rm -rf {цель}")

        def стенд(имя: str, нажатия=(), слова=()) -> dict:
            """Своё окружение: LOG_DIR с журналом входящих и журналом кнопок. Судье его подаёт
            носитель стенда (run/run_вывод с стенд=True); паспорт в окружении — для следа сторожа."""
            логи = pathlib.Path(двор, имя)
            логи.mkdir(parents=True)
            (логи / "inbox.jsonl").write_text("".join(слова), encoding="utf-8")
            (логи / "consent-buttons.jsonl").write_text("".join(нажатия), encoding="utf-8")
            (логи / "consent-requests.jsonl").write_text(
                "".join(_просьба_к(н) for н in нажатия if н.startswith('{"v"')), encoding="utf-8")
            конф = pathlib.Path(двор, f"{имя}.conf")
            конф.write_text(f'LOG_DIR="{логи}"\n' + ПАСПОРТ_ВЛАДЕЛЬЦА, encoding="utf-8")
            окружение = dict(os.environ)
            окружение["HARNESS_INSTALL_CONF"] = str(конф)
            return окружение

        def нажатие(событие: str, минут_назад: float, предмет: str = os.path.realpath(цель)) -> str:
            """Конверт решения — как его пишет диспетчер канала."""
            когда = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=минут_назад)
            данные = {"nonce": f"n{минут_назад}", "вид": "удаление", "цель": предмет, "message_id": 4000,
                      "from_id": ВЛАДЕЛЕЦ_СТЕНДА}
            return json.dumps({"v": 1, "ts": когда.isoformat(timespec="seconds"), "src": "проба",
                               "event": событие, "outcome": "ok", "data": данные}, ensure_ascii=False) + "\n"

        # БОЛЬНОЙ СЛУЧАЙ ПЕРВЫМ: причину написал сам агент, кнопки нет вовсе.
        env_пусто = стенд("log-pusto")
        путей += 1
        if run(команда, env=env_пусто, стенд=True) != 2:
            print("  ПЛОХО причина без кнопки владельца открыла дверь данных")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: причина есть, кнопки «Да» нет — дверь закрыта")

        env = стенд("log-da", нажатия=[нажатие("consent.yes", 1)])
        путей += 1
        if run(команда, env=env, стенд=True) != 0:
            print("  ПЛОХО «Да» кнопкой на цель + причина в команде — дверь данных не открылась")
            bad += 1
        else:
            print("  ок    «Да» кнопкой на цель + причина в команде — дверь открыта")

        # БОЛЬНОЙ СЛУЧАЙ: слово владельца текстом с названным путём, кнопки нет — не открывает.
        слово = json.dumps({"ts": datetime.datetime.now().astimezone().isoformat(), "text":
                            f"Удали секреты и копии базы из {цель}", "message_id": "552"},
                           ensure_ascii=False) + "\n"
        путей += 1
        вывод = run_вывод(команда, env=стенд("log-text", слова=[слово]), стенд=True)
        if вывод[0] != 2:
            print("  ПЛОХО текст владельца с названным путём открыл дверь данных")
            bad += 1
        elif "кнопк" not in вывод[1].lower():
            print(f"  ПЛОХО отказ не называет кнопку: {вывод[1][:200]}")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: текст с названным путём дверь не открывает, отказ называет кнопку")

        # Закрывают: «Нет» после «Да», «Да» на другую цель, «Да» старше срока, отзыв текстом.
        for имя, нажатия, слова in (
                ("«Нет» после «Да»", [нажатие("consent.yes", 3), нажатие("consent.no", 1)], []),
                ("«Да» на ДРУГУЮ цель", [нажатие("consent.yes", 1, "/var/www/другой/secrets")], []),
                ("«Да» старше срока кнопки", [нажатие("consent.yes", 24 * 60)], []),
                ("отзыв текстом после «Да»", [нажатие("consent.yes", 3)],
                 [json.dumps({"ts": datetime.datetime.now().astimezone().isoformat(), "text":
                              "стоп, не удаляй секреты", "message_id": "553"}, ensure_ascii=False) + "\n"])):
            путей += 1
            if run(команда, env=стенд(f"log-{путей}", нажатия=нажатия, слова=слова), стенд=True) != 2:
                print(f"  ПЛОХО {имя} не закрыло дверь данных")
                bad += 1
            else:
                print(f"  ок    {имя} — дверь данных закрыта")

        # Битый журнал кнопок — «прочитать не смогли»: дверь закрыта, причина названа.
        путей += 1
        вывод = run_вывод(команда, env=стенд("log-bitaja", нажатия=["{битая строка\n"]), стенд=True)
        if вывод[0] != 2 or "не прочитан" not in вывод[1]:
            print(f"  ПЛОХО битый журнал кнопок: код {вывод[0]}, причина не названа: {вывод[1][:160]}")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: журнал кнопок битый — дверь закрыта, причина названа")

        # Тот же стенд со свежим «Да» БЕЗ носителя стенда: паспорт стенда в окружении (путь M
        # замера) судье не хозяин — он судит носитель машины, где этой кнопки нет. Рядом контроль:
        # с носителем стенда то же «Да» открыло дверь выше.
        путей += 1
        вывод = run_вывод(команда, env=env)
        if вывод[0] != 2:
            print(f"  ПЛОХО паспорт стенда в окружении открыл дверь данных мимо носителя: код {вывод[0]}")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: подмена паспорта окружением — судья берёт носитель машины, отказ")
        # И ключ прежнего стенда в окружении больше ничего не открывает (спека §3.2).
        путей += 1
        вывод = run_вывод(команда, env=dict(env, HARNESS_OWNER_WORD_STEND="1"))
        if вывод[0] != 2:
            print(f"  ПЛОХО ключ стенда в окружении открыл дверь данных: код {вывод[0]}")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: ключ стенда в окружении + паспорт стенда — отказ")

    return bad, путей


def _judge_cases(cases: list, env: dict, prefix: str, стенд: bool = False) -> tuple[int, int]:
    """Вердикт каждого случая против ожидания; (неудач, путей). `стенд` — как у run."""
    bad = 0
    for want, name, cmd in cases:
        got = run(cmd, env=env, стенд=стенд)
        if got != want:
            bad += 1
        print(f"  {'ок ' if got == want else 'ПЛОХО'} ждали={want} получили={got}  {prefix}: {name}")
    return bad, len(cases)


# Владелец стенда: судья засчитывает «Да» только с from_id = TG_CHAT_ID конфига стенда.
ВЛАДЕЛЕЦ_СТЕНДА = 4242
ПАСПОРТ_ВЛАДЕЛЬЦА = f'TG_CHAT_ID="{ВЛАДЕЛЕЦ_СТЕНДА}"\n'


def _просьба_к(строка_нажатия: str) -> str:
    """Просьба ask-consent к нажатию: тот же вид, цель и nonce, на полминуты раньше — живой порядок."""
    нажатие = json.loads(строка_нажатия)
    когда = datetime.datetime.fromisoformat(нажатие["ts"]) - datetime.timedelta(seconds=30)
    данные = {к: нажатие["data"][к] for к in ("nonce", "вид", "цель")}
    return json.dumps({"v": 1, "ts": когда.isoformat(timespec="seconds"), "src": "scripts/ask-consent.py",
                       "event": "consent.request", "outcome": "ok", "data": {**данные, "почему": "проба"}},
                      ensure_ascii=False) + "\n"


def _button(logs: pathlib.Path, target, event: str = "consent.yes", minutes_ago: float = 1) -> None:
    """Нажатие кнопки вида «удаление» на realpath цели — конверт, как его пишет диспетчер канала,
    с парной просьбой в журнале запросов."""
    when = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)
    real = os.path.realpath(str(target))
    data = {"nonce": f"n-{event}-{abs(hash(real)) % 10**8}", "вид": "удаление", "цель": real,
            "message_id": 4000, "from_id": ВЛАДЕЛЕЦ_СТЕНДА}
    строка = json.dumps({"v": 1, "ts": when.isoformat(timespec="seconds"), "src": "проба",
                         "event": event, "outcome": "ok", "data": data}, ensure_ascii=False) + "\n"
    with open(logs / "consent-requests.jsonl", "a", encoding="utf-8") as fh:
        fh.write(_просьба_к(строка))
    with open(logs / "consent-buttons.jsonl", "a", encoding="utf-8") as fh:
        fh.write(строка)


def check_protected_paths() -> tuple[int, int]:
    """Дыра 1 перепроверки 03.11.2026: охрана удаления судила ИМЯ компонента
    пути («secrets», «data») и два глагола (rm, find -delete), а не сам путь.
    Путь из PROTECTED_PATHS и SECRETS_DIR уходили любым другим глаголом: mv, shred, truncate,
    `: >`, cp /dev/null, dd, rsync --delete, git clean.

    Стенд свой и без слов data/secrets/backup в именах каталогов: иначе отказ
    дало бы старое правило по имени, и проба позеленела бы, не проверив новое.
    Защищённый путь подаётся ключом PROTECTED_PATHS из harness.conf, SECRETS_DIR и
    BACKUP_DIR — из install.conf: путь секретов уже записан там, второй копией
    в другом конфиге он разъедется.
    Дверь — та же, что у удаления данных (HARNESS_DATA_DELETE_WHY в звене
    команды плюс «Да» КНОПКОЙ на realpath цели): хук видит только текст команды,
    переменная окружения до него значением не доезжает, а вторая дверь со своим
    именем разошлась бы с первой. Текст владельца дверь не открывает.
    """
    with tempfile.TemporaryDirectory(prefix="проба-защиты-") as yard:
        parent = pathlib.Path(yard, "sosed")
        project = parent / "chuzhoj-proekt"
        keys = pathlib.Path(yard, "klyuchi")
        copies = pathlib.Path(yard, "kopii")
        logs = pathlib.Path(yard, "log")
        for d in (project, keys, copies, logs):
            d.mkdir(parents=True)
        for f in (keys / "k", keys / "f", keys / "gotov.tmp", copies / "baza.dump"):
            f.write_text("x", encoding="utf-8")
        own_tmp = pathlib.Path(yard, "svoj.tmp")
        own_tmp.write_text("x", encoding="utf-8")
        (logs / "inbox.jsonl").write_text("", encoding="utf-8")
        install = pathlib.Path(yard, "install.conf")
        install.write_text(f'LOG_DIR="{logs}"\nSECRETS_DIR="{keys}"\n'
                           f'BACKUP_DIR="{copies}"\nAUTONOMY="semi"\n' + ПАСПОРТ_ВЛАДЕЛЬЦА, encoding="utf-8")
        harness = pathlib.Path(yard, "harness.conf")
        harness.write_text(f'PROTECTED_PATHS="{project}"\n', encoding="utf-8")
        env = dict(os.environ, HARNESS_INSTALL_CONF=str(install),
                   HARNESS_CONF=str(harness))
        why = "HARNESS_DATA_DELETE_WHY='его слово 03.11'"
        cases = [
            (2, "rm -rf PROTECTED_PATHS", f"rm -rf {project}"),
            (2, "sudo rm -rf PROTECTED_PATHS", f"sudo rm -rf {project}"),
            (2, "mv PROTECTED_PATHS прочь", f"mv {project} /tmp/x"),
            (2, "find PROTECTED_PATHS -delete", f"find {project} -delete"),
            (2, "sudo find PROTECTED_PATHS -mindepth 1 -delete",
             f"sudo find {project} -mindepth 1 -delete"),
            (2, "find PROTECTED_PATHS -exec rm", f"find {project} -type f -exec rm -f {{}} +"),
            (2, "shred файла секретов", f"shred {keys}/f"),
            (2, "truncate -s0 файла секретов", f"truncate -s0 {keys}/f"),
            (2, "': >' поверх СУЩЕСТВУЮЩЕГО файла секретов", f": > {keys}/f"),
            (2, "cp /dev/null поверх файла секретов", f"cp /dev/null {keys}/f"),
            (2, "mv SECRETS_DIR прочь", f"mv {keys} /tmp/x"),
            (2, "dd of= в файл секретов", f"dd if=/dev/zero of={keys}/f bs=1 count=1"),
            (2, "rsync --delete в PROTECTED_PATHS", f"rsync -a --delete /tmp/pusto/ {project}/"),
            (2, "git -C PROTECTED_PATHS clean -fdx", f"git -C {project} clean -fdx"),
            (2, "cd PROTECTED_PATHS && git clean -fdx", f"cd {project} && git clean -fdx"),
            (2, "rm файла из BACKUP_DIR (имя без слова backup)", f"rm -f {copies}/baza.dump"),
            (2, "docker run -v родителя PROTECTED_PATHS и rm внутри",
             f"docker run --rm -v {parent}:/h alpine rm -rf /h/chuzhoj-proekt"),
            (2, "БОЛЬНОЙ СЛУЧАЙ: причина в команде без слова владельца",
             f"{why} rm -rf {project}"),
            # Ревью-A 03.11: предок «/» тоже предок защищённого пути.
            (2, "find / -path 'PROTECTED_PATHS*' -delete", f"find / -path '{project}*' -delete"),
            (2, "docker run -v /:/host и rm внутри",
             f"docker run -v /:/host alpine rm -rf /host{project}"),
            # Обёртки вне списка ведут тот же rm.
            (2, "setsid rm -rf PROTECTED_PATHS", f"setsid rm -rf {project}"),
            (2, "flock /tmp/l rm -rf PROTECTED_PATHS", f"flock /tmp/l rm -rf {project}"),
            (2, "busybox rm -rf PROTECTED_PATHS", f"busybox rm -rf {project}"),
            (2, "su -c 'rm -rf PROTECTED_PATHS'", f"su -c 'rm -rf {project}'"),
            (2, "timeout -s KILL 10 rm -rf PROTECTED_PATHS", f"timeout -s KILL 10 rm -rf {project}"),
            # Группы оболочки: звено начинается не с глагола.
            (2, "{ rm -rf PROTECTED_PATHS; }", f"{{ rm -rf {project}; }}"),
            (2, "if true; then rm -rf PROTECTED_PATHS; fi", f"if true; then rm -rf {project}; fi"),
            # Глаголы, затирающие существующий файл секретов.
            (2, "ln -sf /dev/null поверх файла секретов", f"ln -sf /dev/null {keys}/f"),
            (2, "install /dev/null поверх файла секретов", f"install /dev/null {keys}/f"),
            (2, "tee поверх СУЩЕСТВУЮЩЕГО файла секретов", f"printf x | tee {keys}/f"),
            (2, "sed -i по файлу секретов", f"sed -i s/a/b/ {keys}/f"),
            (2, "mv файла секретов прочь", f"mv {keys}/k /tmp/x"),
            # Контроли: чтение, копирование ИЗ защищённого и НОВЫЙ файл в нём
            # (генератор пароля пишет ключ впервые) свободны до и после правки.
            (0, "cp ИЗ секретов — источник, не цель", f"cp {keys}/k /tmp/x"),
            (0, "cat файла секретов", f"cat {keys}/k"),
            (0, "ls SECRETS_DIR", f"ls {keys}"),
            (0, "mv своего файла из дерева проекта в /tmp",
             "mv /var/www/project/.tasks/x.tmp /tmp"),
            (0, "rm своего временного файла", f"rm {own_tmp}"),
            (0, "git clean -n вне защищённого — сухой прогон",
             "git -C /var/www/project clean -n"),
            (0, "echo > НОВОГО файла в секретах", f"echo x > {keys}/novyj"),
            (0, "docker run -v родителя PROTECTED_PATHS только читает",
             f"docker run --rm -v {parent}:/h alpine ls /h/chuzhoj-proekt"),
            # Атомарная запись нового секрета: временный файл рядом и mv в
            # НЕСУЩЕСТВУЮЩУЮ цель того же каталога ничего не затирает.
            (0, "openssl > S/r.tmp && mv S/r.tmp S/novyj — новый секрет",
             f"openssl rand -hex 16 > {keys}/r.tmp && mv {keys}/r.tmp {keys}/novyj"),
            (0, "mv готового S/*.tmp в несуществующую цель того же каталога",
             f"mv {keys}/gotov.tmp {keys}/novyj"),
        ]
        bad, paths = _judge_cases(cases, env, "защищённый путь", стенд=True)
        # И-3: имя файла в каталоге секретов само бывает ключом доступа —
        # отказ называет корень защищённого, а не путь к файлу.
        secret_name = "sekret-klyuch-777"  # не-секрет: выдуманное имя для пробы утечки
        (keys / secret_name).write_text("x", encoding="utf-8")
        for verb, cmd in (("truncate -s0", f"truncate -s0 {keys}/{secret_name}"),
                          (": >", f": > {keys}/{secret_name}"),
                          ("shred", f"shred {keys}/{secret_name}")):
            got, said = run_вывод(cmd, env=env, стенд=True)
            ok = got == 2 and secret_name not in said
            if not ok:
                bad += 1
            print(f"  {'ок ' if ok else 'ПЛОХО'} rc={got} имя_в_выводе="
                  f"{secret_name in said}  защищённый путь: отказ «{verb}» без имени файла секрета")
        paths += 3
        # Текст владельца с названными путями дверь не открывает; «Да» кнопкой на цель — открывает.
        (logs / "inbox.jsonl").write_text(json.dumps({
            "ts": datetime.datetime.now().astimezone().isoformat(),
            "chat_id": "1000000001", "message_id": "553",
            "текст": f"Удали копии и секреты из {project} и {keys}"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        b_text, p_text = _judge_cases([(2, "дверь: причина + ТЕКСТ с путём без кнопки — закрыта",
                                        f"{why} rm -rf {project}")], env, "защищённый путь", стенд=True)
        (logs / "inbox.jsonl").write_text("", encoding="utf-8")
        _button(logs, project)
        _button(logs, keys)
        door = [
            (0, "дверь: причина + «Да» кнопкой на цель — rm -rf PROTECTED_PATHS",
             f"{why} rm -rf {project}"),
            (0, "дверь: та же дверь и для другого глагола — mv SECRETS_DIR",
             f"{why} mv {keys} /tmp/x"),
        ]
        bad_d, paths_d = _judge_cases(door, env, "защищённый путь", стенд=True)
    return bad + bad_d + b_text, paths + paths_d + p_text


def _owner_word_line(text: str) -> str:
    """Свежая строка входящих с признаком авторства телеграма (message_id)."""
    return json.dumps({"ts": datetime.datetime.now().astimezone().isoformat(),
                       "chat_id": "1000000001", "message_id": "554", "текст": text},
                      ensure_ascii=False) + "\n"


def _trace_records(logs: pathlib.Path) -> list[dict]:
    """Записи guard_exceptions.log стенда; битая строка — не запись."""
    log = logs / "guard_exceptions.log"
    records = []
    for line in (log.read_text(encoding="utf-8").splitlines() if log.is_file() else []):
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    return records


def _judge_trace(logs: pathlib.Path, wanted, name: str, prefix: str) -> int:
    """Есть ли в следе стенда запись, о которой судит `wanted`; 1 — неудача."""
    ok = any(wanted(r) for r in _trace_records(logs))
    print(f"  {'ок ' if ok else 'ПЛОХО'} след={ok}  {prefix}: {name}")
    return 0 if ok else 1


def check_delete_door_targets() -> tuple[int, int]:
    """Цели карточки hr.delete-door-covers-only-data-dirs: ключи ~/.ssh, архивы
    продукта, чужие проекты. 27.10.2026 `sudo -n rm -rf /var/www/app-архивы`
    унёс 754 журнала и не оставил следа: правило по имени знает только
    data/backup/secrets, а защиту по пути эти каталоги не получили.

    Защита этих целей — ДАННЫЕ (ключ PROTECTED_PATHS), и стенд подаёт свой
    список, свой HOME и свои журналы; в именах каталогов нет data/backup/secrets,
    иначе отказ дало бы правило по имени. Ниже же — дыры двери: «~» в команде,
    кнопка на чужую цель, find -delete без двери, корень-предок рабочих мест.
    """
    bad = paths = 0
    with tempfile.TemporaryDirectory(prefix="проба-целей-двери-") as raw_yard:
        yard = pathlib.Path(os.path.realpath(raw_yard))
        home = yard / "home"
        ssh = home / ".ssh"
        arch = yard / "www" / "prod-arhivy"
        arch_ru = yard / "www" / "прод-архивы"
        foreign = yard / "www" / "chuzhoj"
        logs = yard / "log"
        for d in (ssh, arch, arch_ru, foreign, logs, yard / "scratchpad" / "y"):
            d.mkdir(parents=True)
        for f in (ssh / "github_x", ssh / "config", ssh / "x", arch / "a.log",
                  arch_ru / "a.log", foreign / "index.html"):
            f.write_text("x", encoding="utf-8")
        inbox = logs / "inbox.jsonl"
        inbox.write_text("", encoding="utf-8")
        install = yard / "install.conf"
        install.write_text(f'LOG_DIR="{logs}"\nAUTONOMY="semi"\n' + ПАСПОРТ_ВЛАДЕЛЬЦА, encoding="utf-8")
        harness = yard / "harness.conf"
        harness.write_text(f'PROTECTED_PATHS="~/.ssh {arch} {arch_ru} {foreign}"\n',
                           encoding="utf-8")
        env = dict(os.environ, HOME=str(home), HARNESS_INSTALL_CONF=str(install),
                   HARNESS_CONF=str(harness))
        why = "HARNESS_DATA_DELETE_WHY='его слово 04.11'"
        cases = [
            (2, "rm -f ~/.ssh/github_x", "rm -f ~/.ssh/github_x"),
            (2, "mv ~/.ssh/github_x прочь", "mv ~/.ssh/github_x /tmp/y"),
            (2, "shred ~/.ssh/github_x", "shred ~/.ssh/github_x"),
            (2, "bash -c 'rm -rf ~/.ssh'", "bash -c 'rm -rf ~/.ssh'"),
            (2, "rm -rf ~ — родитель ~/.ssh", "rm -rf ~"),
            (2, "rm -rf архивов продукта", f"rm -rf {arch}"),
            (2, "sudo -n rm -rf \"архивы\" — замер 27.10", f'sudo -n rm -rf "{arch}"'),
            (2, "find архивов -delete", f"find {arch} -delete"),
            (2, "rm -rf архивов с кириллицей в имени", f"rm -rf {arch_ru}"),
            (2, "sudo -n rm -rf \"архивы\" с кириллицей", f'sudo -n rm -rf "{arch_ru}"'),
            (2, "find архивов с кириллицей -delete", f"find {arch_ru} -delete"),
            (2, "rm -rf чужого проекта", f"rm -rf {foreign}"),
            (2, "mv чужого проекта прочь", f"mv {foreign} /tmp/z"),
            (2, "git -C чужой-проект clean -fdx", f"git -C {foreign} clean -fdx"),
            (2, "БОЛЬНОЙ СЛУЧАЙ: причина без слова владельца", f"{why} rm -rf {foreign}"),
            # Свободно при том же списке: рабочие места, чтение, новый файл.
            (0, "rm -rf /tmp/x", "rm -rf /tmp/x"),
            (0, "rm -rf scratchpad/y стенда", f"rm -rf {yard}/scratchpad/y"),
            (0, "rm в .tasks/…/tmp", "rm -rf /var/www/project/.tasks/hr.x/tmp/y"),
            (0, "cat ~/.ssh/config — чтение", "cat ~/.ssh/config"),
            (0, "cp ~/.ssh/x /tmp/ — источник, не цель", "cp ~/.ssh/x /tmp/"),
            (0, "ssh-keyscan >> НОВОГО файла в ~/.ssh",
             "ssh-keyscan github.com >> ~/.ssh/known_hosts_novyj"),
        ]
        b, p = _judge_cases(cases, env, "цели двери", стенд=True)
        bad, paths = bad + b, paths + p

        # Список — данные: тот же снос без ключа проходит, мутация данных меняет вердикт.
        empty = yard / "harness-pusto.conf"
        empty.write_text('PROTECTED_PATHS=""\n', encoding="utf-8")
        b, p = _judge_cases([(0, "PROTECTED_PATHS=\"\" — rm -rf чужого проекта проходит",
                              f"rm -rf {foreign}")],
                            dict(env, HARNESS_CONF=str(empty)), "цели двери, данные", стенд=True)
        bad, paths = bad + b, paths + p

        # Дверь: причина + «Да» кнопкой на realpath цели; проход оставляет след.
        for cmd, target in ((f"{why} rm -f ~/.ssh/github_x", ssh / "github_x"),
                            (f"{why} rm -rf {arch_ru}", arch_ru)):
            _button(logs, target)
            b, p = _judge_cases([(0, f"дверь: «Да» кнопкой на {target.name} + причина", cmd)], env,
                                "цели двери", стенд=True)
            bad, paths = bad + b, paths + p
            bad += _judge_trace(
                logs, lambda r, t=str(target): r.get("reason") == "дверь: удаление данных"
                and t in r.get("detail", ""),
                f"след «дверь: удаление данных» с путём {target.name}", "цели двери")
            paths += 1

        # Дыры двери: «~» в команде раскрывается в HOME и судится по realpath; кнопка на чужую цель
        # и текст владельца с названным путём — не разрешение.
        inbox.write_text(_owner_word_line(f"снеси проект {foreign} и удали архивы {arch}"),
                         encoding="utf-8")
        holes = [
            (0, "«~» в команде раскрывается в HOME: кнопка на realpath открывает",
             f"{why} rm -f ~/.ssh/github_x"),
            (2, "БОЛЬНОЙ СЛУЧАЙ: текст «снеси проект <путь>» без кнопки не открывает",
             f"{why} rm -rf {foreign}"),
            (2, "БОЛЬНОЙ СЛУЧАЙ: текст «удали архивы <путь>» без кнопки не открывает",
             f"{why} rm -rf {arch}"),
            (2, "кнопка на архивы-кириллицу не открывает чужой проект", f"{why} rm -rf {foreign}"),
            # Дверь образа отката закрыта наглухо: ни текст, ни путь её не открывают.
            (2, "текст владельца не открывает дверь :previous",
             "HARNESS_PREVIOUS_DELETE_WHY='его слово 04.11' docker rmi app-api:previous"),
        ]
        for want, name, cmd in holes:
            b, p = _judge_cases([(want, name, cmd)], env, "дыры двери", стенд=True)
            bad, paths = bad + b, paths + p

        # find -delete по каталогу из имён данных: дверь та же, что у rm.
        named = yard / "srv" / "data"
        named.mkdir(parents=True)
        (named / "baza.dump").write_text("x", encoding="utf-8")
        inbox.write_text("", encoding="utf-8")
        b, p = _judge_cases([
            (2, "find data -delete без причины", f"find {named} -delete"),
            (2, "БОЛЬНОЙ СЛУЧАЙ: find data -delete, причина без слова",
             f"{why} find {named} -delete")], env, "find-дверь", стенд=True)
        bad, paths = bad + b, paths + p
        inbox.write_text("", encoding="utf-8")
        _button(logs, named)
        b, p = _judge_cases([(0, "find data -delete: причина + «Да» кнопкой на цель",
                              f"{why} find {named} -delete")], env, "find-дверь", стенд=True)
        bad, paths = bad + b, paths + p
        bad += _judge_trace(
            logs, lambda r: r.get("reason") == "дверь: удаление данных"
            and str(named) in r.get("detail", "") and "find" in r.get("detail", ""),
            "след «дверь: удаление данных» с путём find", "find-дверь")
        paths += 1
    b, p = _check_door_all_targets()
    bad, paths = bad + b, paths + p
    b, p = _check_blockade_roots()
    return bad + b, paths + p


def _check_door_all_targets() -> tuple[int, int]:
    """Дверь по имени судит КАЖДУЮ цель каждого звена: «Да» кнопкой на a/data не открывает
    b/secrets; причина в чужом звене не считается; на несколько целей нужна кнопка на каждую.
    """
    with tempfile.TemporaryDirectory(prefix="проба-двери-всех-целей-") as raw_yard:
        yard = pathlib.Path(os.path.realpath(raw_yard))
        a, b_ = yard / "a" / "data", yard / "b" / "secrets"
        logs = yard / "log"
        for d in (a, b_, logs):
            d.mkdir(parents=True)
        inbox = logs / "inbox.jsonl"
        install = yard / "install.conf"
        install.write_text(f'LOG_DIR="{logs}"\nAUTONOMY="semi"\n' + ПАСПОРТ_ВЛАДЕЛЬЦА, encoding="utf-8")
        harness = yard / "harness.conf"
        harness.write_text("", encoding="utf-8")
        env = dict(os.environ, HOME=str(yard / "home"), HARNESS_INSTALL_CONF=str(install),
                   HARNESS_CONF=str(harness))
        why = "HARNESS_DATA_DELETE_WHY='его слово 04.11'"
        inbox.write_text("", encoding="utf-8")
        _button(logs, a)
        cases = [
            (0, "кнопка на a/data: rm одной цели", f"{why} rm -rf {a}"),
            (0, "кнопка на a/data: find одной цели", f"{why} find {a} -delete"),
            (2, "find A B -delete", f"{why} find {a} {b_} -delete"),
            (2, "find A -delete; find B -delete", f"{why} find {a} -delete; find {b_} -delete"),
            (2, "find A -delete с переводом строки", f"{why} find {a} -delete\nfind {b_} -delete"),
            (2, "find A -delete | find B -delete", f"{why} find {a} -delete | find {b_} -delete"),
            (2, "find A -delete; WHY=y find B -delete",
             f"{why} find {a} -delete; HARNESS_DATA_DELETE_WHY=y find {b_} -delete"),
            (2, "rm -rf A B", f"{why} rm -rf {a} {b_}"),
            (2, "rm -rf A; rm -rf B", f"{why} rm -rf {a}; rm -rf {b_}"),
            (2, "rm -rf A && rm -rf B", f"{why} rm -rf {a} && rm -rf {b_}"),
            (2, "rm -rf A с переводом строки", f"{why} rm -rf {a}\nrm -rf {b_}"),
            # «..» в пути цели не прячет чужой каталог под названный.
            (2, "find A/../../b/secrets -delete", f"{why} find {a}/../../b/secrets -delete"),
            (2, "rm -rf A/../../b/secrets", f"{why} rm -rf {a}/../../b/secrets"),
            # Перенос «\» + перевод строки склеивает строки: B остаётся целью.
            (2, "rm -rf A \\⏎ B", f"{why} rm -rf {a} \\\n {b_}"),
            (2, "find A \\⏎ B -delete", f"{why} find {a} \\\n {b_} -delete"),
            # Причина в чужом звене не открывает дверь соседнего.
            (2, "причина в соседнем звене: true; rm", f"{why} true; rm -rf {a}"),
            (2, "причина в соседнем звене: true && rm", f"{why} true && rm -rf {a}"),
            (2, "причина в соседнем звене: true; find", f"{why} true; find {a} -delete"),
        ]
        bad, paths = _judge_cases(cases, env, "дверь, все цели", стенд=True)
        # Кнопка на КАЖДУЮ цель: законная дверь на несколько целей; на родителя одной кнопки мало.
        _button(logs, b_)
        b, p = _judge_cases([
            (0, "кнопки на обе цели: rm -rf A B", f"{why} rm -rf {a} {b_}"),
            (0, "кнопки на обе цели: find A B -delete", f"{why} find {a} {b_} -delete")],
            env, "дверь, общий родитель", стенд=True)
        bad, paths = bad + b, paths + p
        bad += _judge_trace(
            logs, lambda r: r.get("reason") == "дверь: удаление данных"
            and str(b_) in r.get("detail", ""), "след двери с несколькими целями",
            "дверь, общий родитель")
        paths += 1
        # Кнопка на родителя обеих целей и текст про родителя — не разрешение на детей.
        inbox.write_text(_owner_word_line(f"Удали копии базы из {yard}"), encoding="utf-8")
        (logs / "consent-buttons.jsonl").write_text("", encoding="utf-8")
        _button(logs, yard)
        b, p = _judge_cases([(2, "кнопка на родителя не открывает детей", f"{why} rm -rf {a} {b_}")],
                            env, "дверь, выбор слова", стенд=True)
        bad, paths = bad + b, paths + p
    return bad, paths


def _check_blockade_roots() -> tuple[int, int]:
    """Риск Р-1 разведки: корень-предок рабочих мест в PROTECTED_PATHS отменял бы
    `> $LOG_DIR/x.log`, sed -i в проекте и запись в HOME — всех сразу (LOG_DIR
    в списке дал 8% отмен за день). Такой корень игнорируется со следом, а
    соседний нормальный корень в том же ключе работает.
    """
    with tempfile.TemporaryDirectory(prefix="проба-блокады-") as raw_yard:
        yard = pathlib.Path(os.path.realpath(raw_yard))
        base = yard / "var"
        project = base / "www" / "proekt"
        logs = base / "log" / "harness"
        home = yard / "home"
        foreign = yard / "oth" / "chuzhoj"
        for d in (project, logs, home, foreign):
            d.mkdir(parents=True)
        for f in (project / "f.txt", logs / "x.log", home / ".bashrc", foreign / "a"):
            f.write_text("x", encoding="utf-8")
        (logs / "inbox.jsonl").write_text("", encoding="utf-8")
        install = yard / "install.conf"
        install.write_text(f'PROJECT_DIR="{project}"\nLOG_DIR="{logs}"\nAUTONOMY="semi"\n',
                           encoding="utf-8")
        harness = yard / "harness.conf"
        harness.write_text(f'PROTECTED_PATHS="{base} ~ /tmp {foreign}"\n', encoding="utf-8")
        env = dict(os.environ, HOME=str(home), HARNESS_INSTALL_CONF=str(install),
                   HARNESS_CONF=str(harness))
        bad, paths = _judge_cases([
            (0, "> $LOG_DIR/x.log при предке LOG_DIR в списке", f"echo x > {logs}/x.log"),
            (0, "sed -i в PROJECT_DIR при предке проекта в списке",
             f"sed -i s/a/b/ {project}/f.txt"),
            (0, ">> ~/.bashrc при ~ в списке", "echo x >> ~/.bashrc"),
            (0, "rm -rf /tmp/x при /tmp в списке", "rm -rf /tmp/x"),
            (2, "соседний нормальный корень в том же ключе держит", f"rm -rf {foreign}"),
        ], env, "блокада", стенд=True)
        # Корень судится целым словом: «/tmp» — префикс любого пути стенда.
        for root in (base, home, pathlib.Path("/tmp")):
            bad += _judge_trace(
                logs, lambda r, t=str(root): r.get("reason") != "дверь: удаление данных"
                and t in re.split(r"[\s|,;:«»\"'()]+",
                                  f"{r.get('reason', '')} {r.get('detail', '')}"),
                f"след: корень {root} проигнорирован", "блокада")
            paths += 1
        # Нормальный корень не попадает в след; след на корень — раз в сутки.
        ignored = [r for r in _trace_records(logs)
                   if r.get("reason") == "корень защиты проигнорирован: над рабочим местом"]
        ok = not any(str(foreign) in r.get("detail", "") for r in ignored)
        print(f"  {'ок ' if ok else 'ПЛОХО'} нормальный корень не в следе игнора  блокада")
        bad += 0 if ok else 1
        details = [r.get("detail") for r in ignored]
        repeats = [d for d in set(details) if details.count(d) > 1]
        print(f"  {'ок ' if not repeats else 'ПЛОХО'} след игнора не чаще раза в сутки "
              f"({len(ignored)} записей)  блокада")
        bad += 1 if repeats else 0
        paths += 2
        only_root = yard / "harness-koren.conf"
        only_root.write_text('PROTECTED_PATHS="/"\n', encoding="utf-8")
        b, p = _judge_cases([(0, "«/» по-прежнему отсекается", "rm -rf /tmp/x")],
                            dict(env, HARNESS_CONF=str(only_root)), "блокада", стенд=True)
        bad, paths = bad + b, paths + p
        # Рабочее место берётся только абсолютным: пустой и «.» не гасят корень,
        # внутри которого стоит cwd хука.
        keep = yard / "harness-sosed.conf"
        keep.write_text(f'PROTECTED_PATHS="{foreign}"\n', encoding="utf-8")
        прежний_cwd = os.getcwd()
        try:
            os.chdir(foreign)
            for name, line in (("PROJECT_DIR не задан", ""), ("PROJECT_DIR пустой", 'PROJECT_DIR=""\n'),
                               ("PROJECT_DIR=«.»", 'PROJECT_DIR="."\n')):
                inst = yard / "install-cwd.conf"
                inst.write_text(f'{line}LOG_DIR="{logs}"\nAUTONOMY="semi"\n', encoding="utf-8")
                b, p = _judge_cases([(2, f"cwd в корне, {name}: корень держит", f"rm -rf {foreign}")],
                                    dict(env, HARNESS_CONF=str(keep), HARNESS_INSTALL_CONF=str(inst)),
                                    "блокада", стенд=True)
                bad, paths = bad + b, paths + p
        finally:
            os.chdir(прежний_cwd)
    return bad, paths


def check_consent_log_writes() -> tuple[int, int]:
    """Дыра 2 перепроверки 03.11.2026: ворота согласия верят журналу
    $LOG_DIR/confirmations.jsonl и сырым $LOG_DIR/inbox/*.txt, а их дописывает
    тот же пользователь, что и смена: `echo … >> confirmations.jsonl` подделывал
    «да» владельца. Законный писатель один — диспетчер канала, из оболочки
    сессии записи туда нет. Чтение и перенос прочитанного в inbox/обработано/
    (так смена отмечает сообщение прочитанным) свободны.
    LOG_DIR стенда свой: путь журнала охрана берёт из конфига, а не литералом.
    """
    with tempfile.TemporaryDirectory(prefix="проба-согласий-") as yard:
        logs = pathlib.Path(yard, "log")
        (logs / "inbox" / "обработано").mkdir(parents=True)
        log = logs / "confirmations.jsonl"
        log.write_text('{"text":"да"}\n', encoding="utf-8")
        (logs / "inbox.jsonl").write_text("", encoding="utf-8")
        (logs / "consent-buttons.jsonl").write_text("", encoding="utf-8")
        (logs / "удаления-секретов.jsonl").write_text("", encoding="utf-8")
        (logs / "consent-requests.jsonl").write_text("", encoding="utf-8")
        msg = logs / "inbox" / "1.txt"
        msg.write_text("да", encoding="utf-8")
        (logs / "inbox" / "обработано" / "старый.txt").write_text("да", encoding="utf-8")
        install = pathlib.Path(yard, "install.conf")
        install.write_text(f'LOG_DIR="{logs}"\nAUTONOMY="semi"\n', encoding="utf-8")
        harness = pathlib.Path(yard, "harness.conf")
        harness.write_text("", encoding="utf-8")
        env = dict(os.environ, HARNESS_INSTALL_CONF=str(install), HARNESS_CONF=str(harness))
        done = logs / "inbox" / "обработано"
        # Как у DEPLOY_GUARD: в пакете модуль в ../scripts/lib, на сервере — в ../lib.
        lib = next(p for p in (_BASE.parent / "scripts" / "lib", _BASE.parent / "lib")
                   if (p / "consent_buttons.py").is_file())
        nonce = "A" * 22
        sent = '{"ok":true,"result":{"message_id":5}}'
        press = ('{"from":{"id":777000},"message":{"chat":{"id":777000},"message_id":5},'
                 f'"data":"c:{nonce}:y"}}')
        cases = [
            (2, "echo >> confirmations.jsonl", f"echo x >> {log}"),
            (2, "tee -a confirmations.jsonl", f"printf x | tee -a {log}"),
            (2, "sed -i по confirmations.jsonl", f"sed -i s/a/b/ {log}"),
            (2, "cp поверх confirmations.jsonl", f"cp /tmp/x {log}"),
            (2, "mv поверх confirmations.jsonl", f"mv /tmp/x {log}"),
            (2, "truncate -s0 confirmations.jsonl", f"truncate -s0 {log}"),
            (2, "': >' confirmations.jsonl", f": > {log}"),
            (2, "echo > inbox/1.txt", f"echo x > {msg}"),
            (2, "tee inbox/1.txt", f"printf x | tee {msg}"),
            # Сверх списка заказа: inbox.jsonl читает дверь удаления данных как
            # слово владельца — дописка туда открывала бы ту дверь подделкой.
            (2, "echo >> inbox.jsonl", f"echo x >> {logs}/inbox.jsonl"),
            # Ревью-A 03.11: стереть согласие — то же, что подделать.
            (2, "rm confirmations.jsonl", f"rm {log}"),
            (2, "rm inbox.jsonl", f"rm {logs}/inbox.jsonl"),
            (2, "rm inbox/1.txt", f"rm {msg}"),
            (2, "mv inbox/1.txt мимо обработано/", f"mv {msg} /tmp/"),
            (2, "rm -r inbox", f"rm -r {logs}/inbox"),
            # hr.consent-buttons: «да» кнопкой дверь удаления берёт из журнала
            # кнопок, а погашение — из следа удалений; вырезанная строка следа
            # снова открыла бы погашенное «да».
            (2, "echo >> consent-buttons.jsonl", f"echo '{{\"событие\":\"да\"}}' >> {logs}/consent-buttons.jsonl"),
            (2, "sed -i по consent-buttons.jsonl", f"sed -i s/нет/да/ {logs}/consent-buttons.jsonl"),
            (2, "echo >> удаления-секретов.jsonl", f"echo x >> {logs}/удаления-секретов.jsonl"),
            (2, "sed -i по удаления-секретов.jsonl", f"sed -i /a/d {logs}/удаления-секретов.jsonl"),
            # Пишущие команды модуля кнопок кладут в журнал то, что им подали:
            # из оболочки сессии они — подделка «да» без нажатия.
            (2, "consent_buttons.py sent из оболочки",
             f"echo '{sent}' | python3 scripts/lib/consent_buttons.py sent {logs} {nonce}"),
            (2, "consent_buttons.py judge из оболочки (абсолютный путь)",
             f"echo '{press}' | python3 {lib}/consent_buttons.py judge {logs} 777000"),
            (2, "consent_buttons.py judge из каталога модуля",
             f"cd {lib} && python3 consent_buttons.py judge {logs} 777000 < /tmp/cb.json"),
            (2, "python3 -m consent_buttons judge", f"PYTHONPATH={lib} python3 -m consent_buttons judge {logs} 777000"),
            (2, "python3 -c: import consent_buttons; judge(...)",
             f"python3 -c \"import sys; sys.path.insert(0, '{lib}'); import consent_buttons as c; "
             f"c.judge(c.Path('{logs}'), '777000', {{}})\""),
            (2, "python3 -c: from consent_buttons import append_row",
             f"python3 -c 'import sys; sys.path.insert(0, \"scripts/lib\"); from consent_buttons import append_row'"),
            (2, "python3 - <<EOF: consent_buttons.record_sent",
             f"python3 - <<'EOF'\nimport sys\nsys.path.insert(0, '{lib}')\nimport consent_buttons\n"
             f"consent_buttons.record_sent(consent_buttons.Path('{logs}'), '{nonce}', '{sent}')\nEOF"),
            # Цель «да» не должна зависеть от журнала запросов, но и правка его
            # после показа владельцу — тот же класс подмены.
            (2, "sed -i по consent-requests.jsonl", f"sed -i s/old/secret/ {logs}/consent-requests.jsonl"),
            (2, "echo >> consent-requests.jsonl", f"echo x >> {logs}/consent-requests.jsonl"),
            (0, "consent_buttons.py unsent (только чтение)", f"python3 scripts/lib/consent_buttons.py unsent {logs} /tmp"),
            (0, "grep по consent_buttons.py", f"grep -n judge {lib}/consent_buttons.py"),
            (0, "проба кнопок test_consent_buttons.py", "python3 scripts/test_consent_buttons.py --случай охрана"),
            (0, "ask-consent.py — законный писатель запросов",
             "python3 scripts/ask-consent.py --цель /tmp/x --почему проба"),
            (0, "cat consent-requests.jsonl", f"cat {logs}/consent-requests.jsonl"),
            (0, "cat consent-buttons.jsonl", f"cat {logs}/consent-buttons.jsonl"),
            (0, "rm из inbox/обработано/ — очистка прочитанного", f"rm {done}/старый.txt"),
            (0, "echo >> другого журнала в LOG_DIR", f"echo x >> {logs}/другой.log"),
            (0, "cat confirmations.jsonl", f"cat {log}"),
            (0, "tail -n 3 confirmations.jsonl", f"tail -n 3 {log}"),
            (0, "grep да confirmations.jsonl", f"grep да {log}"),
            (0, "cat inbox/1.txt", f"cat {msg}"),
            (0, "mv inbox/1.txt в обработано/ — отметка прочитанного", f"mv {msg} {done}/"),
            (0, "mv inbox/1.txt в обработано/1.txt", f"mv {msg} {done}/1.txt"),
        ]
        return _judge_cases(cases, env, "журнал согласий")


def проверить_дверь_previous() -> tuple[int, int]:
    """Дверь удаления образа отката закрыта наглухо; заодно — дверь чтения живого учёта.

    Случай 28.09.2026: владелец велел снять продукт с сервера целиком, и дверь «:previous»
    открывалась его словом. С 06.11.2026 текст владельца необратимое не открывает, а кнопки
    на образ нет (предмет не путь, гашение требует docker — беклог): дверь закрыта для всего,
    и проба судит ровно это. Дверь чтения учёта обратима и остаётся текстовой.
    """
    bad = 0
    путей = 0
    случаи = [
        (2, "пустая причина дверь не открывает",
         "HARNESS_PREVIOUS_DELETE_WHY='' docker rmi app-api:previous"),
        (2, "причина из одних пробелов — не слово",
         "HARNESS_PREVIOUS_DELETE_WHY='   ' docker rmi app-api:previous"),
        (2, "без причины отказ как прежде", "docker rmi app-api:previous"),
        (0, "дверь открывает только этот путь, latest и так разрешён",
         "docker rmi app-api:latest"),
    ]
    # Переменная НЕ передаётся через окружение нарочно: хук получает от Claude
    # Code только текст команды, и дверь по os.environ не открылась бы никогда
    # (проба 28.09.2026 — первая редакция двери именно так и не сработала).
    env = dict(os.environ)
    env["HARNESS_PREVIOUS_DELETE_WHY"] = "окружение дверь открывать не должно"
    путей += 1
    if run("docker rmi app-api:previous", env=env) != 2:
        print("  ПЛОХО дверь открылась ОКРУЖЕНИЕМ, а не словом в команде")
        bad += 1
    else:
        print("  ок    окружение дверь не открывает — причина только в команде")
    for ждём, имя, cmd in случаи:
        путей += 1
        код = run(cmd)
        if код != ждём:
            print(f"  ПЛОХО {имя}: ждали {ждём}, вышло {код}")
            bad += 1
        else:
            print(f"  ок    {имя}")
    # Стенд создаёт своё условие: свой LOG_DIR с МАШИННЫМ журналом диспетчера, боевые
    # входящие не читаются (их читают двери чтения учёта ниже).
    # Каталог журнала судье подаёт ТОЛЬКО носитель стенда в процессе пробы (спека §3.2):
    # окружение его не подменяет.
    команда = ("HARNESS_PREVIOUS_DELETE_WHY='его слово 28.09' "
               "docker rmi app-api:previous")

    def строка_журнала(текст: str, минут_назад: float = 0,
                       message_id: str | None = "551") -> str:
        когда = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=минут_назад)
        запись = {"ts": когда.isoformat(), "chat_id": "1000000001", "текст": текст}
        if message_id is not None:
            запись["message_id"] = message_id
        return json.dumps(запись, ensure_ascii=False) + "\n"

    with tempfile.TemporaryDirectory() as двор:
        логи = pathlib.Path(двор, "log")
        логи.mkdir(parents=True)
        журнал = логи / "inbox.jsonl"
        журнал.write_text(
            строка_журнала("Надо все с сервера очистить полносттю, удали образы"),
            encoding="utf-8")
        конф = pathlib.Path(двор, "install.conf")
        конф.write_text(f'LOG_DIR="{логи}"\n', encoding="utf-8")
        env = dict(os.environ)
        env["HARNESS_INSTALL_CONF"] = str(конф)
        # Сначала — БОЛЬНОЙ СЛУЧАЙ: входящих нет вовсе, причину написал сам
        # агент. Судится на СВОЁМ дворе: на боевых входящих свежее слово
        # владельца сделало бы этот путь зелёным по чужой причине.
        пусто = pathlib.Path(двор, "log-pusto")
        пусто.mkdir(parents=True)
        (пусто / "inbox.jsonl").write_text("", encoding="utf-8")
        конф_пусто = pathlib.Path(двор, "install-pusto.conf")
        конф_пусто.write_text(f'LOG_DIR="{пусто}"\n', encoding="utf-8")
        env_пусто = dict(env)
        env_пусто["HARNESS_INSTALL_CONF"] = str(конф_пусто)
        путей += 1
        if run("HARNESS_PREVIOUS_DELETE_WHY='агент сам себе написал причину' "
               "docker rmi app-api:previous", env=env_пусто, стенд=True) != 2:
            print("  ПЛОХО причина без слова владельца открыла дверь")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: причина есть, слова владельца нет — дверь закрыта")
        # Дверь :previous закрыта НАГЛУХО (06.11.2026): образ — не путь, «Да» кнопкой к нему не
        # привязать, а текст владельца необратимое не открывает. Свежее слово с названным
        # предметом и причиной в команде — отказ, и отказ говорит об этом.
        путей += 1
        вывод = run_вывод(команда, env=env, стенд=True)
        if вывод[0] != 2:
            print("  ПЛОХО со свежим словом владельца дверь :previous открылась")
            bad += 1
        elif "наглухо" not in вывод[1]:
            print(f"  ПЛОХО отказ не говорит, что дверь закрыта наглухо: {вывод[1][:200]}")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: свежее слово + причина — дверь :previous закрыта наглухо")
        журнал.unlink()
        путей += 1
        if run(команда, env=env, стенд=True) != 2:
            print("  ПЛОХО без журнала входящих дверь :previous открылась")
            bad += 1
        else:
            print("  ок    без журнала входящих дверь :previous закрыта")

        # ДВЕРЬ ЧТЕНИЯ ЖИВОГО УЧЁТА. Больной случай ЖИВОЙ, 29.09.2026: владелец
        # выбрал вариант «2», потом написал «читай статусы и типы цен» — сторож
        # отменил чтение оба раза, потому что брал словарь двери УДАЛЕНИЯ и искал
        # «удали»/«токен». Дверь, которую нельзя открыть ничем, — не дверь.
        чтение = ("HARNESS_ERP_READ_WHY='снять форму ответа справочников' "
                  "curl -s https://api.erp-obrazec.test/api/remap/1.2/entity/store")
        журнал.write_text(строка_журнала("2"), encoding="utf-8")
        путей += 1
        if run(чтение, env=env, стенд=True) != 2:
            print("  ПЛОХО цифра «2» открыла дверь чтения учёта")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: ответ цифрой дверь чтения не открывает")
        журнал.write_text(строка_журнала("удали токен"), encoding="utf-8")
        путей += 1
        if run(чтение, env=env, стенд=True) != 2:
            print("  ПЛОХО слово об УДАЛЕНИИ открыло дверь ЧТЕНИЯ")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: слово удаления дверь чтения не открывает")
        журнал.write_text(строка_журнала("читай статусы и типы цен"), encoding="utf-8")
        путей += 1
        if run(чтение, env=env, стенд=True) != 0:
            print("  ПЛОХО «читай статусы и типы цен» + причина — дверь чтения не открылась")
            bad += 1
        else:
            print("  ок    свежее «читай …» + причина в команде — дверь чтения открыта")
        путей += 1
        if run("curl -s https://api.erp-obrazec.test/api/remap/1.2/entity/store", env=env, стенд=True) != 2:
            print("  ПЛОХО чтение БЕЗ причины в команде открылось по одному его слову")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: его слово есть, причины в команде нет — закрыто")
        журнал.write_text(строка_журнала("не читай статусы пока"), encoding="utf-8")
        путей += 1
        if run(чтение, env=env, стенд=True) != 2:
            print("  ПЛОХО отказ «не читай» открыл дверь чтения")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: «не читай» дверь не открывает")
        # Разрешение на чтение НЕ открывает дверь удаления: словари развели
        # нарочно, и один класс действий не берёт слово другого.
        журнал.write_text(строка_журнала("читай статусы и типы цен"), encoding="utf-8")
        путей += 1
        if run(команда, env=env, стенд=True) != 2:
            print("  ПЛОХО слово о ЧТЕНИИ открыло дверь УДАЛЕНИЯ образа")
            bad += 1
        else:
            print("  ок    БОЛЬНОЙ СЛУЧАЙ: слово чтения дверь удаления не открывает")
    return bad, путей


def проверить_публикацию_мимо_двери() -> tuple[int, int]:
    """Пары красный/зелёный на правило «публикация только через publish.sh».

    Имена удалённого и каталога сборки — ДАННЫЕ harness.conf, поэтому стенд
    подсовывает свой конфиг: правило, зашитое на «jig», молчало бы у другой
    установки, и проба этого бы не заметила.
    """
    bad = 0
    путей = 0
    with tempfile.TemporaryDirectory() as tmp:
        конф = pathlib.Path(tmp, "harness.conf")
        конф.write_text('PUBLIC_REMOTE="мойпублик"\n'
                        f'PUBLIC_BUILD_DIR="{tmp}/сборка"\n', encoding="utf-8")
        # Ворота отвечают «можно» (заглушка): правило публикации судится само по себе, а не
        # молчанием ворот — без ответа ворот push необратим и отказывается (fail-closed).
        проект = pathlib.Path(tmp, "проект")
        (проект / "scripts").mkdir(parents=True)
        (проект / "scripts" / "deploy_guard.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        пасп = pathlib.Path(tmp, "install.conf")
        пасп.write_text(f'PROJECT_DIR="{проект}"\n', encoding="utf-8")
        env = dict(os.environ)
        env["HARNESS_CONF"] = str(конф)
        env["HARNESS_INSTALL_CONF"] = str(пасп)
        случаи = [
            (2, "push в публичный удалённый отменён",
             "git push мойпублик HEAD:master"),
            (2, "push из каталога публичной сборки отменён",
             f"git -C {tmp}/сборка push origin HEAD:master"),
            (2, "тег в публичный удалённый — тоже публикация",
             "git push мойпублик refs/tags/v0.3.0"),
            (0, "дверь публикации разрешена",
             "bash scripts/publish.sh"),
            (0, "обычный push в свой origin не задет",
             "git push origin master"),
            (0, "имя удалённого внутри другого слова не считается",
             "git push origin мойпубликатор-ветка"),
        ]
        for ждём, имя, cmd in случаи:
            путей += 1
            код = run(cmd, env=env)
            if код != ждём:
                print(f"  ПЛОХО {имя}: ждали {ждём}, вышло {код} — {cmd}")
                bad += 1
            else:
                print(f"  ок    {имя}")
        # Ключей в конфиге нет — правило МОЛЧИТ, а не запрещает всё (ворота ответили «можно»).
        пустой = pathlib.Path(tmp, "пустой.conf")
        пустой.write_text("# без ключей публикации\n", encoding="utf-8")
        env_пустой = dict(env)
        env_пустой["HARNESS_CONF"] = str(пустой)
        путей += 1
        код = run("git push мойпублик HEAD:master", env=env_пустой)
        if код != 0:
            print(f"  ПЛОХО без ключей публикации правило обязано молчать: {код}")
            bad += 1
        else:
            print("  ок    без ключей публикации правило молчит")
    return bad, путей


def проверить_сеть_мимо_двери() -> tuple[int, int]:
    """G1–G8: push в общий репозиторий сети — только дверью evolution-net.py --отправить.

    Ручной push в клон HarnessEvolution проходил сторож целиком (факт 9 плана фазы 1):
    коммит уходил ко всем серверам без суда подписи и без закреплённого адреса.
    Клон и адрес — ДАННЫЕ harness.conf, поэтому стенд подсовывает свой конфиг; ворота
    выката — заглушка «можно», чтобы отказ шёл от правила, а не от молчания ворот.
    """
    bad = 0
    путей = 0
    sha = "0123456789abcdef" * 2 + "01234567"
    with tempfile.TemporaryDirectory() as tmp:
        клон = pathlib.Path(tmp, "клон-сети")
        (клон / ".git").mkdir(parents=True)
        ссылка = pathlib.Path(tmp, "ссылка-на-клон")
        ссылка.symlink_to(клон)
        # без «.git»: соседний «…HarnessEvoStand-other.git» содержит его подстрокой (G3d)
        адрес = "git@github.com:Example/HarnessEvoStand"
        конф = pathlib.Path(tmp, "harness.conf")
        конф.write_text(f'EVOLUTION_DIR="{клон}"\nEVOLUTION_REMOTE_URL="{адрес}"\n', encoding="utf-8")
        проект = pathlib.Path(tmp, "проект")
        (проект / "scripts").mkdir(parents=True)
        (проект / "scripts" / "deploy_guard.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        пасп = pathlib.Path(tmp, "install.conf")
        пасп.write_text(f'PROJECT_DIR="{проект}"\n', encoding="utf-8")
        env = dict(os.environ)
        env["HARNESS_CONF"] = str(конф)
        env["HARNESS_INSTALL_CONF"] = str(пасп)
        случаи = [
            # G1 — краснеет, если разбор звена `git -C` снят (push=None): путь клона в тексте
            # НЕ назван, его видит только разбор звена (относительный -C после cd в родителя)
            (2, "G1 push из клона сети через относительный -C отменён",
             f"cd {tmp} && git -C {клон.name} push origin {sha}:refs/heads/main"),
            # G1s — краснеет, если каталог сравнивается без realpath (E-15): симлинк обходит правило
            (2, "G1s push через симлинк на клон отменён",
             f"git -C {ссылка} push origin {sha}:refs/heads/main"),
            # G2 — краснеет, если снят разбор cd в звеньях: каталог клона относительный
            (2, "G2 cd в клон по относительному пути и push отменён",
             f"cd {tmp} && cd {клон.name} && git push"),
            # G3 — краснеет, если снят поиск адреса EVOLUTION_REMOTE_URL в команде
            (2, "G3 push по адресу общего репозитория из другого каталога отменён",
             f"git push {адрес} {sha}:refs/heads/main"),
            # G3b/G3c/G3f — краснеет, если адрес сравнивается как строка, а не как репозиторий
            (2, "G3f тот же репозиторий с «.git» отменён",
             f"git push git@github.com:Example/HarnessEvoStand.git {sha}:refs/heads/main"),
            (2, "G3b тот же репозиторий по ssh:// отменён",
             f"git push ssh://git@github.com/Example/HarnessEvoStand.git {sha}:refs/heads/main"),
            (2, "G3c тот же репозиторий по https:// без .git отменён",
             f"git push https://github.com/Example/HarnessEvoStand {sha}:refs/heads/main"),
            # G3d — краснеет, если соседний репозиторий судится как этот (подстрока адреса без .git)
            (0, "G3d соседний репозиторий …HarnessEvoStand-other.git не задет",
             f"git push git@github.com:Example/HarnessEvoStand-other.git {sha}:refs/heads/main"),
            (0, "G3e соседний каталог …/клон-сети-other не задет",
             f"cd {клон}-other && git push origin master"),
            # G4 — краснеет, если снят разбор GIT_DIR= звена: путь относительный
            (2, "G4 относительный GIT_DIR клона и push отменён",
             f"cd {tmp} && GIT_DIR={клон.name}/.git git push"),
            # G9 — краснеет, если send-pack не считается отправкой
            (2, "G9 git send-pack из клона отменён",
             f"cd {tmp} && git -C {клон.name} send-pack origin {sha}:refs/heads/main"),
            (2, "G9b git send-pack по адресу отменён",
             f"git send-pack {адрес} {sha}:refs/heads/main"),
            # G10 — краснеет, если `-c alias.X=push` не считается отправкой
            (2, "G10 алиас push через -c в клоне отменён",
             f"cd {tmp} && git -C {клон.name} -c alias.p=push p origin {sha}:refs/heads/main"),
            # G5 — краснеет, если правило шире нужного: push проекта судят ворота выката, не оно
            (0, "G5 push проекта не задет",
             f"cd {проект} && git push origin master"),
            # G6 — краснеет, если правило перекрыло саму дверь
            (0, "G6 дверь сети разрешена",
             f"python3 scripts/evolution-net.py --отправить {sha}"),
            # G8 — краснеет, если правило режет любой git в клоне, а не только push
            (0, "G8 fetch клона сети не задет",
             f"git -C {клон} fetch origin"),
        ]
        for ждём, имя, cmd in случаи:
            путей += 1
            код = run(cmd, env=env)
            if код != ждём:
                print(f"  ПЛОХО {имя}: ждали {ждём}, вышло {код}")
                bad += 1
            else:
                print(f"  ок    {имя}")
        # G1t — краснеет, если отказ молчит о единственном пути (AC-17): человек не узнает, как надо
        путей += 1
        код, текст = run_вывод(f"git -C {клон} push origin {sha}:refs/heads/main", env=env)
        if код != 2 or "evolution-net.py --отправить" not in текст:
            print(f"  ПЛОХО G1t отказ называет дверь evolution-net.py --отправить: код {код}")
            bad += 1
        else:
            print("  ок    G1t отказ называет дверь evolution-net.py --отправить")
        # G7 — краснеет, если правило без ключей EVOLUTION_* запрещает push вообще
        пустой = pathlib.Path(tmp, "пустой.conf")
        пустой.write_text("# без ключей сети серверов\n", encoding="utf-8")
        env_пустой = dict(env)
        env_пустой["HARNESS_CONF"] = str(пустой)
        путей += 1
        код = run(f"git -C {клон} push origin {sha}:refs/heads/main", env=env_пустой)
        if код != 0:
            print(f"  ПЛОХО G7 без ключей EVOLUTION_* правило обязано молчать: {код}")
            bad += 1
        else:
            print("  ок    G7 без ключей EVOLUTION_* правило молчит")
    return bad, путей


def проверить_гашение_по_выборке() -> tuple[int, int]:
    """Гашение контейнеров ПО ВЫБОРКЕ с именем продукта — отказ (их ac18dac).

    Улика соседней установки 17.10.2026: исполнитель погасил боевую витрину выборкой
    `docker ps | grep <продукт> | xargs docker kill`, выкат и откат её не
    подняли, сайт лежал ~7 минут. Имя продукта — ДАННЫЕ harness.conf
    (PRODUCT_DIR и PRODUCT_ALIASES), поэтому стенд подаёт свой конфиг.

    Граница имени — `(?![\\w-])`, не `\\b`: их 3.1 — `\\b` стоит и между «a» и
    «-», и свой стенд `shopfront-ms2` судился как продукт. Отказ обязан назвать
    имя продукта: код 2 от чужого правила этот случай не закрывает.
    """
    bad = 0
    путей = 0
    with tempfile.TemporaryDirectory() as tmp:
        нет_install = str(pathlib.Path(tmp, "нет-такого.conf"))

        def среда(текст: str, имя: str) -> dict:
            конф = pathlib.Path(tmp, имя)
            конф.write_text(текст, encoding="utf-8")
            env = dict(os.environ)
            env["HARNESS_CONF"] = str(конф)
            env["HARNESS_INSTALL_CONF"] = нет_install
            return env

        по_прозвищу = среда('PRODUCT_DIR=""\nPRODUCT_ALIASES="shopfront"\n', "прозвище.conf")
        по_каталогу = среда('PRODUCT_DIR="shopfront"\nPRODUCT_ALIASES=""\n', "каталог.conf")
        без_продукта = среда("# продукта нет\n", "пусто.conf")
        случаи = [
            # (ждём, имя, команда, среда)
            (2, "БОЛЬНОЙ ac18dac: docker stop $(docker ps --filter name=<продукт>)",
             "docker stop $(docker ps -q --filter name=shopfront)", по_прозвищу),
            (2, "БОЛЬНОЙ ac18dac: docker kill $(docker ps -f name=<продукт>)",
             "docker kill $(docker ps -q -f name=shopfront)", по_прозвищу),
            (2, "БОЛЬНОЙ ac18dac: docker rm -f по выборке, фильтр в кавычках",
             'docker rm -f $(docker ps -aq --filter "name=shopfront")', по_прозвищу),
            (2, "БОЛЬНОЙ ac18dac: выборка через xargs docker stop",
             "docker ps -q --filter name=shopfront | xargs docker stop", по_прозвищу),
            (2, "БОЛЬНОЙ ac18dac: форма аварии — docker ps | grep <продукт> | xargs docker kill",
             "docker ps | grep shopfront | awk '{print $1}' | xargs docker kill", по_прозвищу),
            (2, "БОЛЬНОЙ ac18dac: имя продукта из PRODUCT_DIR (так оно задано на боевой)",
             "docker stop $(docker ps -q --filter name=shopfront)", по_каталогу),
            (0, "их 3.1: свой стенд <продукт>-ms2 гасится — граница (?![\\w-])",
             "docker stop $(docker ps -q --filter name=shopfront-ms2)", по_прозвищу),
            (0, "их 3.1: стенд <продукт>-stand через xargs гасится",
             "docker ps -q --filter name=shopfront-stand | xargs docker stop", по_прозвищу),
            (0, "их 3.1: docker compose -p <продукт>-stand down — свой стенд",
             "docker compose -p shopfront-stand down", по_прозвищу),
            (0, "подчёркивание — часть имени: shopfront_x не продукт",
             "docker stop $(docker ps -q --filter name=shopfront_x)", по_прозвищу),
            (0, "выборка без гашения (docker logs) не задета",
             "docker logs $(docker ps -q --filter name=shopfront)", по_прозвищу),
            (0, "чтение списка контейнеров продукта не задето",
             "docker ps --filter name=shopfront", по_прозвищу),
            (0, "продукт не объявлен — правило молчит (fail-open)",
             "docker stop $(docker ps -q --filter name=shopfront)", без_продукта),
            # Пустой список имён собрал бы шаблон «(?:)», а он совпадает с
            # пустым местом в любой выборке: гасить ВСЁ стало бы нельзя на
            # установке без продукта.
            (0, "продукт не объявлен: docker stop $(docker ps -q ) — правило молчит",
             "docker stop $(docker ps -q )", без_продукта),
            (0, "продукт не объявлен: docker ps -aq | xargs docker stop — правило молчит",
             "docker ps -aq | xargs docker stop", без_продукта),
        ]
        for ждём, имя, cmd, env in случаи:
            путей += 1
            код, текст = run_вывод(cmd, env=env)
            верно = код == ждём and (ждём == 0 or "shopfront" in текст)
            if not верно:
                print(f"  ПЛОХО {имя}: ждали {ждём}"
                      f"{' и имя продукта в отказе' if ждём else ''}, вышло {код} — {cmd}")
                bad += 1
            else:
                print(f"  ок    {имя}")
    return bad, путей


def проверить_прогон_в_сессии() -> int:
    """Прогон только фоном (указание владельца 23.08.2026).

    Смотрим на текст причины: во время чужого прогона сторож отвечает двойкой
    и за «прогон уже идёт», и код сам по себе ничего не различает.
    """
    плохо = 0
    for ждём_отказ, описание, cmd in ФОНОВЫЕ_СЛУЧАИ:
        p = subprocess.run(
            GUARD,
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
            capture_output=True, text=True,
        )
        отказ = p.returncode == 2 and ПРИЧИНА_В_СЕССИИ in p.stderr
        if отказ == ждём_отказ:
            print(f"  ок  прогон фоном: {описание}")
        else:
            print(f"  ПЛОХО  прогон фоном: {описание} — ждали отказ={ждём_отказ}, вышло {отказ}")
            плохо += 1
    return плохо


def проверить_гнездо_tmux() -> tuple[int, int]:
    """Дверь (в): гнездо без живого владельца удалять можно, с живым — нет.

    Условие создаётся здесь: два каталога tmux-* со своими сокетами — один держит
    слушающий сокет, у другого сокет-файл остался от умершего сервера. Агентский
    /tmp/tmux-<uid> не трогается. Заодно сверяется «одна правда»: префикс, который
    бережёт сторож, обязан стоять в списке защищённых имён уборщика /tmp.
    """
    import shutil
    import socket
    плохо = 0
    путей = 0
    ядро = pathlib.Path(__file__).resolve().parents[2]
    списки = [ядро / "harness" / "config" / "tmp-sweep.yaml",
              ядро / "config" / "tmp-sweep.yaml"]
    текст = next((c.read_text(encoding="utf-8") for c in списки if c.exists()), "")
    путей += 1
    if '"tmux-"' not in текст:
        print("  ПЛОХО  tmux-: уборщик /tmp не называет префикс защищённым (или список не найден)")
        плохо += 1
    else:
        print("  ок  tmux-: префикс гнезда назван защищённым в tmp-sweep.yaml")
    жив = tempfile.mkdtemp(prefix="tmux-w2live-", dir="/tmp")
    мёртв = tempfile.mkdtemp(prefix="tmux-w2dead-", dir="/tmp")
    слушатель = socket.socket(socket.AF_UNIX)
    бывший = socket.socket(socket.AF_UNIX)
    try:
        слушатель.bind(f"{жив}/default")
        слушатель.listen(1)
        бывший.bind(f"{мёртв}/default")
        бывший.listen(1)
        бывший.close()                    # файл остаётся, слушателя нет
        среда = dict(os.environ, TMPDIR="/tmp")
        for ждём, описание, команда, слово in [
            (2, "живой сервер в каталоге: rm -rf отменён", f"rm -rf {жив}", "kill-server"),
            (2, "живой сокет прямым путём отменён", f"rm {жив}/default", "tmp-sweep"),
            (2, "живой: find по гнезду -delete отменён", f"find {жив} -delete", ""),
            (0, "умерший: find по гнезду -delete пропущен", f"find {мёртв} -delete", ""),
            (2, "живой: cd /tmp и относительное имя", f"cd /tmp && rm -rf {os.path.basename(жив)}", ""),
            (2, "живой: путь через /./", f"rm -rf /tmp/./{os.path.basename(жив)}", ""),
            (2, "живой: путь через $TMPDIR", f"rm -rf $TMPDIR/{os.path.basename(жив)}", ""),
            (2, "живой: mv уносит гнездо", f"mv {жив} /tmp/w2-унесено", ""),
            (2, "живой: unlink сокета", f"unlink {жив}/default", ""),
            (2, "живой: в bash -c тоже", f"bash -c 'rm -rf {жив}'", ""),
            (0, "умерший: mv гнезда пропущен", f"mv {мёртв} {мёртв}-x", ""),
            (0, "каталог умершего сервера: дверь (в) открыта", f"rm -rf {мёртв}", ""),
            (0, "несуществующее гнездо: удалять нечего", "rm -rf /tmp/tmux-w2-net-такого", ""),
        ]:
            путей += 1
            код, вывод = run_вывод(команда, env=среда)
            ок = код == ждём and (слово in вывод)
            if ок:
                print(f"  ок  дверь (в): {описание}")
            else:
                print(f"  ПЛОХО  дверь (в): {описание} — код {код}, слово «{слово}» "
                      f"{'есть' if слово in вывод else 'нет'} в отказе")
                плохо += 1
    finally:
        слушатель.close()
        shutil.rmtree(жив, ignore_errors=True)
        shutil.rmtree(мёртв, ignore_errors=True)
    return плохо, путей


def проверить_гнездо_по_uid() -> tuple[int, int]:
    """Шаблон и выборка find судятся по гнезду ТОГО пользователя, чей агент.

    Больной случай 30.10.2026: установка под uid 1005, а образец гнезда был
    вшит «tmux-1004» — шаблон tmux-1005* и find с исключением ЧУЖОГО гнезда
    проходили мимо сторожа. Стенд берёт заведомо не свой uid (свой + 1) через
    HARNESS_OWN_UID, чтобы проба не зависела от того, кто её запускает.
    """
    плохо = 0
    стенд = os.getuid() + 1
    чужой = os.getuid()                 # гнездо запускающего — для стенда чужое
    среда = dict(os.environ, HARNESS_OWN_UID=str(стенд))
    случаи = [
        (2, "шаблон по гнезду пользователя", f"rm -rf /tmp/tmux-{стенд}*"),
        (2, "find -path по гнезду пользователя", f"find /tmp -path '/tmp/tmux-{стенд}*' -delete"),
        (2, "исключено чужое гнездо, своё нет", f"find /tmp ! -name 'tmux-{чужой}' -delete"),
        (0, "исключено своё гнездо", f"find /tmp -mindepth 1 ! -name 'tmux-{стенд}' ! -path '*/tmux-{стенд}/*' -delete"),
    ]
    for ждём, описание, команда in случаи:
        код, _ = run_вывод(команда, env=среда)
        if код == ждём:
            print(f"  ок  гнездо uid {стенд}: {описание}")
        else:
            print(f"  ПЛОХО  гнездо uid {стенд}: {описание} — ждали {ждём}, вышло {код}")
            плохо += 1
    return плохо, len(случаи)


def _загрузить_сторожа():
    """Модуль сторожа — ИЗ ТОГО ЖЕ файла, что запускает run_процессом.

    Через GUARD_UNDER_TEST мутационная проба подсовывает испорченную копию;
    импортируй мы имя пакета, проба гоняла бы боевой файл и всегда зеленела.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("guard_под_тестом", GUARD[1])
    модуль = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(модуль)
    return модуль


ГВАРД = _загрузить_сторожа()

def проверить_подсказку_pgrep() -> tuple[int, int]:
    """Подсказка сторожа обязана ДАВАТЬ РАБОЧИЙ шаблон, а не литерал.

    БОЛЬНОЙ СЛУЧАЙ 28.09.2026: команда разом запускала прогон и звала
    «pgrep -af "[g]ates.sh"». Отказ верен (шаблон совпадает с запуском в той же
    строке), но подсказка оборачивала уже обёрнутое и советовала
    «[[]g]ates.sh» — это ищет ЛИТЕРАЛ «[g]ates.sh», которого нет ни в одной
    командной строке. Смена прочитала живой прогон мёртвым и держала три
    прогона разом.
    """
    плохо, путей = 0, 0
    случаи = [
        ("gates.sh", "[g]ates.sh"),
        ("[g]ates.sh", "[g]ates.sh"),
        ("[a]cceptanc[e]", "[a]cceptance"),
    ]
    for шаблон, ждём in случаи:
        путей += 1
        вышло = ГВАРД.обезврежен(шаблон)
        if вышло == ждём:
            print(f"  ок  подсказка pgrep: «{шаблон}» → «{вышло}»")
        else:
            print(f"  ПЛОХО  подсказка pgrep: «{шаблон}» → «{вышло}», ждали «{ждём}»")
            плохо += 1
    # Боевой путь целиком: отказ на составной команде, и в тексте — рабочая форма.
    путей += 1
    p = subprocess.run(
        GUARD,
        input=json.dumps({"tool_name": "Bash", "tool_input": {
            "command": 'bash scripts/gates.sh --всё & pgrep -af "[g]ates.sh"'}}),
        capture_output=True, text=True,
    )
    если_литерал = "[[]" in p.stderr
    if p.returncode == 2 and 'pgrep -af "[g]ates.sh"' in p.stderr and not если_литерал:
        print("  ок  БОЛЬНОЙ СЛУЧАЙ: в отказе рабочая форма, а не литерал «[[]g]…»")
    else:
        print(f"  ПЛОХО  отказ советует литерал: код {p.returncode}, «[[]» есть={если_литерал}")
        плохо += 1
    # БОЛЬНОЙ СЛУЧАЙ п.4 отчёта чужого сервера 25.10: шаблон УЖЕ с классом, а имя
    # стоит литералом в другом звене той же команды. Отказ верен — замер
    # 26.10: `bash -c "pgrep -af '[z]z-…'; echo zz-…"` нашёл саму оболочку
    # bash -c. Но отказ советовал «классом символов, он ломает
    # самосовпадение: pgrep -af "[t]g-dispatcher"» — ту самую форму, что и
    # отменил. Подсказка обязана назвать, ЧТО совпало (литерал в другом
    # звене), и не обещать, что класс поможет: он уже стоит.
    путей += 1
    p = subprocess.run(
        GUARD,
        input=json.dumps({"tool_name": "Bash", "tool_input": {
            "command": 'pgrep -af "[t]g-dispatcher" || echo "нет tg-dispatcher"'}}),
        capture_output=True, text=True,
    )
    своя_часть = p.stderr.split("Сторож отменяет ВСЮ")[0]
    назвал_литерал = "«tg-dispatcher»" in своя_часть
    обещает_класс = "ломает самосовпадение" in своя_часть
    if p.returncode == 2 and назвал_литерал and not обещает_класс:
        print("  ок  БОЛЬНОЙ п.4: шаблон уже с классом — отказ называет совпавший литерал,"
              " класс не обещает")
    else:
        print(f"  ПЛОХО  БОЛЬНОЙ п.4: код {p.returncode}, назван литерал «tg-dispatcher»="
              f"{назвал_литерал}, обещает класс={обещает_класс} — отказ советует ту же форму")
        плохо += 1
    return плохо, путей


def проверить_подсказку_носителя() -> tuple[int, int]:
    """Отказ claude мимо носителя обязан дать ГОТОВУЮ команду носителя с флагом.

    K29 контракта hr.standards-and-policy, Н-4: носитель --strict-mcp-config сам
    не добавляет, и подсказка без флага вела бы к вызову, который отбирает канал
    у владельца. Готовая команда из подсказки обязана проходить самого сторожа.
    """
    плохо, путей = 0, 0
    for cmd in ("claude -p --strict-mcp-config x", 'claude -p "сделай X"', 'out=$(claude -p "x")'):
        путей += 1
        код, текст = run_вывод(cmd)
        своя_часть = текст.split("Сторож отменяет ВСЮ")[0]
        беды = []
        if код != 2:
            беды.append(f"отказа нет (код {код})")
        if "claude-run.sh --класс прибор" not in своя_часть:
            беды.append("в подсказке нет «claude-run.sh --класс прибор»")
        if "--strict-mcp-config" not in своя_часть:
            беды.append("в подсказке нет «--strict-mcp-config»")
        совет = re.search(r"bash \S*claude-run\.sh --класс прибор[^\n«»]*", своя_часть)
        if совет and run_процессом(совет.group(0).rstrip(" .;,")) != 0:
            беды.append(f"готовая команда «{совет.group(0)}» сторожем отменена")
        if беды:
            print(f"  ПЛОХО  подсказка носителя на «{cmd}» — {'; '.join(беды)}")
            плохо += 1
        else:
            print(f"  ок  подсказка носителя на «{cmd}»: claude-run.sh --класс прибор и --strict-mcp-config")
    return плохо, путей


def проверить_совет_проходит() -> tuple[int, int]:
    """Совет сторожа обязан проходить самого сторожа.

    БОЛЬНОЙ СЛУЧАЙ п.4 отчёта другой установки, ветка литерала (замер 28.10): шаблон без
    класса, а имя стоит литералом и в другом звене команды (путь лога, строка
    echo). Отказ верен, но советует «классом символов, он ломает
    самосовпадение» — подставленный в ту же команду, этот совет сторож
    отменяет снова: литерал в другом звене никуда не делся.
    Правило судится поведением: для каждого совета `pgrep -af "<X>"` из отказа
    (1) сам совет отдельной командой проходит; (2) либо отказ велит вынести
    проверку отдельной командой, либо команда с подставленным советом проходит.
    """
    имя = "tg-" + "dispatcher"
    случаи = [  # (команда, шаблон в ней как написан, имя случая, совет обязан подставляться)
        (f"until pgrep -f {имя}; do sleep 1; done; tail -3 /var/log/harness/{имя}.log", имя,
         "БОЛЬНОЙ п.4-литерал: until pgrep -f <имя> …; tail …/<имя>.log", False),
        (f'pgrep -af {имя} || echo "нет {имя}"', имя,
         "БОЛЬНОЙ п.4-литерал: pgrep -af <имя> || echo \"нет <имя>\"", False),
        ("until pgrep -f gates.sh; do sleep 5; done", "gates.sh",
         "сосед: имя только в шаблоне — совет подставляется в ту же команду", True),
        (f'pgrep -af "[t]g-dispatcher" || echo "нет {имя}"', '"[t]g-dispatcher"',
         "сосед: шаблон уже с классом, имя в другом звене", False),
    ]
    плохо, путей = 0, 0
    for cmd, шаблон, назв, подставлять in случаи:
        путей += 1
        код, текст = run_вывод(cmd)
        своя_часть = текст.split("Сторож отменяет ВСЮ")[0]
        советы = re.findall(r'^\s+pgrep -af "([^"]+)"', своя_часть, re.M)
        беды = []
        if код != 2:
            беды.append(f"отказа нет (код {код})")
        if not советы:
            беды.append("в отказе нет совета pgrep -af")
        отдельно = "отдельной командой" in своя_часть
        if подставлять and отдельно:
            беды.append("имени вне шаблона нет, а отказ велит выносить проверку отдельной командой")
        for совет in советы:
            один = f'pgrep -af "{совет}"'
            if run_процессом(один) != 0:
                беды.append(f"совет «{один}» отдельной командой отменён")
            if not отдельно:
                подставлено = cmd.replace(шаблон, f'"{совет}"', 1)
                if run_процессом(подставлено) != 0:
                    беды.append(f"совет, подставленный в ту же команду, отменён: «{подставлено}»")
        if беды:
            print(f"  ПЛОХО  {назв} — {'; '.join(беды)}")
            плохо += 1
        else:
            print(f"  ок  {назв}")
    return плохо, путей


def run_процессом(cmd: str, tool: str = "Bash", env: dict | None = None) -> int:
    """Настоящий запуск: проверяет контракт процесса целиком."""
    p = subprocess.run(
        GUARD,
        input=json.dumps({"tool_name": tool, "tool_input": {"command": cmd}}),
        capture_output=True, text=True, env=env,
    )
    return p.returncode


def run_вывод(cmd: str, tool: str = "Bash", env: dict | None = None,
              стенд: bool = False) -> tuple[int, str]:
    """Код И ТЕКСТ отказа: дверь, молчащая о причине, неотличима от сломанной.

    Отдельно от `run`: тот судит только вердикт и ради скорости зовёт сторожа
    внутри процесса, а текст отказа сторож печатает в свой stderr.
    `стенд` — судье носитель стенда из паспорта env (обёртка), а не машины.
    """
    p = subprocess.run(
        зов_обёртки(GUARD[1], env) if стенд else GUARD,
        input=json.dumps({"tool_name": tool, "tool_input": {"command": cmd}}),
        capture_output=True, text=True, env=env,
    )
    return p.returncode, (p.stderr or "") + (p.stdout or "")


_СКАЗАНО: set[str] = set()


def run(cmd: str, tool: str = "Bash", env: dict | None = None, стенд: bool = False) -> int:
    """Тот же вердикт, но без запуска процесса.

    221 путь × 107 мс отдельного python = 24 с из 41 с всех ворот (замер
    11.09.2026). Окружение сторож читает ВНУТРИ функций и конфиг не кэширует —
    подмена os.environ здесь равносильна env= у subprocess (проверено тем, что
    пути про ворота выката и ротацию остались зелёными).
    Не-Bash вызовы отсекает main(), а не check(), — их гоняем процессом.
    `стенд` — судье на время вызова носитель стенда из паспорта env, как в обёртке.
    """
    if tool != "Bash":
        return run_процессом(cmd, tool, env)
    судья = ГВАРД.слово_вл
    прежний_носитель = судья.__dict__.get("носитель")
    if стенд:
        try:
            носитель = носитель_стенда(судья, ГВАРД.конф, env["HARNESS_INSTALL_CONF"], env.get("HARNESS_CONF"))
        except LookupError as беда:
            if str(беда) not in _СКАЗАНО:
                _СКАЗАНО.add(str(беда))
                print(f"  ПЛОХО обёртка пробы: {беда} — случаи стенда судить нечем, их код {КОД_ОБЁРТКИ}")
            return КОД_ОБЁРТКИ
    прежнее = dict(os.environ)
    прежняя_сессия = getattr(ГВАРД, "СЕССИЯ", "")
    try:
        if env is not None:
            os.environ.clear()
            os.environ.update(env)
        if стенд:
            судья.носитель = lambda: носитель
        ГВАРД.СЕССИЯ = ""
        try:
            return ГВАРД.check(cmd)
        except SystemExit as e:          # сторож вправе выйти сам
            return int(e.code or 0)
        except Exception:
            # main() ловит исключения сторожа: обратимое пропускает (fail-open), НЕОБРАТИМОЕ
            # отказывает — внутрипроцессный вызов обязан вести себя так же, иначе тест
            # объявил бы красным то, что в бою зелено.
            return 2 if ГВАРД.необратимое_в(cmd) else 0
    finally:
        os.environ.clear()
        os.environ.update(прежнее)
        ГВАРД.СЕССИЯ = прежняя_сессия
        if прежний_носитель is None:
            судья.__dict__.pop("носитель", None)
        else:
            судья.носитель = прежний_носитель


def gate_env(tmp: str, busy: bool) -> dict:
    """Временный PROJECT_DIR с фейковыми воротами + конфиг, подсунутый через окружение."""
    proj = pathlib.Path(tmp, "proj")
    (proj / "scripts").mkdir(parents=True, exist_ok=True)
    (proj / "scripts" / "deploy_guard.py").write_text(
        FAKE_GUARD_BUSY if busy else FAKE_GUARD_FREE, encoding="utf-8"
    )
    conf = pathlib.Path(tmp, "install.conf")
    conf.write_text(
        '# тестовый конфиг\n'
        'PROJECT_NAME="testproj"\n'
        f'PROJECT_DIR="{proj}"\n'
        'AUTONOMY="semi"\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["HARNESS_INSTALL_CONF"] = str(conf)
    return env


def среда_без_ротации(tmp: str) -> dict:
    """Окружение, где журнала ротаций нет: сторож обязан запрещать уход.

    Без этого CASES читали БОЕВОЙ $LOG_DIR/rotation.jsonl, и стоило сторожу
    сессий начать смену, как одиннадцать путей «уход без смены» краснели —
    тест зависел бы от того, что происходит на машине в эту минуту (та же
    беда, что чинили 11.08.2026 вынесением путей без конфига в NOCONF_CASES).
    """
    conf = pathlib.Path(tmp, "install.conf")
    conf.write_text(f'LOG_DIR="{pathlib.Path(tmp, "пусто")}"\nAUTONOMY="semi"\n',
                    encoding="utf-8")
    env = dict(os.environ)
    env["HARNESS_INSTALL_CONF"] = str(conf)
    return env


def среда_с_ротацией(tmp: str, событие: str, возраст_сек: int) -> dict:
    """Окружение с журналом ротаций: последнее событие задаётся случаем."""
    log_dir = pathlib.Path(tmp, "log")
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S+05:00",
                       time.localtime(time.time() - возраст_сек))
    (log_dir / "rotation.jsonl").write_text(
        json.dumps({"ts": ts, "event": событие, "session": "s", "detail": "проба"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    conf = pathlib.Path(tmp, "install.conf")
    conf.write_text(f'LOG_DIR="{log_dir}"\nAUTONOMY="semi"\n', encoding="utf-8")
    env = dict(os.environ)
    env["HARNESS_INSTALL_CONF"] = str(conf)
    return env


# Уход из сессии, когда смену ВЕДЁТ сторож. Больной случай 12.08.2026: сторож
# подал поручение «выйди /exit, не зови --rotate-now», а этот сторож его
# отменял — поручение нельзя было исполнить ничем. Первым идёт случай, где
# запрет обязан остаться: без него правка открыла бы дыру 11.08 (22:51).
ROTATION_CASES = [
    (2, "БОЛЬНОЙ: rotate_command СТАРЫЙ (час назад) — смену никто не ведёт, уход запрещён",
     "rotate_command", 3600),
    (2, "последнее событие не ротация (agent_restarted) — уход запрещён",
     "agent_restarted", 10),
    (0, "свежий rotate_command — смену ведёт сторож и ждёт выхода, /exit разрешён",
     "rotate_command", 10),
]


# ── проба 13 hr.consent-buttons-for-every-yes: сторож передаёт воротам предмет пуша ──
# Кнопка судится, только если ВСЯ команда — одно звено `git [-C dir] push <удалённый>
# <40-hex>:refs/heads/<ветка>`; тогда ворота получают `--пуш <удалённый> <refspec> <repo>`
# тремя значениями. Любая другая форма — без `--пуш` (spec.md §5, Н-1, Н-16).
FAKE_GUARD_ARGV = (
    "import json, os, sys\n"
    "with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'argv.jsonl'), 'a') as fh:\n"
    "    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
    "sys.exit(0)\n"
)


def check_push_subject_hook() -> tuple[int, int]:
    bad = paths = 0
    sha = "0123456789abcdef0123456789abcdef01234567"
    ref = f"{sha}:refs/heads/master"
    with tempfile.TemporaryDirectory(prefix="проба-сторож-пуш-") as tmp:
        root = pathlib.Path(tmp).resolve()
        proj = root / "proj"
        (proj / "scripts").mkdir(parents=True)
        (proj / "scripts" / "deploy_guard.py").write_text(FAKE_GUARD_ARGV, encoding="utf-8")
        argv_log = proj / "scripts" / "argv.jsonl"
        repo, elsewhere = root / "repo", root / "elsewhere"
        repo.mkdir()
        elsewhere.mkdir()
        (root / "link").symlink_to(repo)
        conf = root / "install.conf"
        conf.write_text(f'PROJECT_DIR="{proj}"\nLOG_DIR="{root}/log"\nAUTONOMY="semi"\n', encoding="utf-8")
        env = dict(os.environ, HARNESS_INSTALL_CONF=str(conf), HARNESS_GUARD_NOLOG="1")

        def gate_calls(cmd: str, cwd: pathlib.Path) -> tuple[int, list]:
            argv_log.unlink(missing_ok=True)
            p = subprocess.run(GUARD, input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
                               capture_output=True, text=True, env=env, cwd=cwd, timeout=60)
            rows = ([json.loads(s) for s in argv_log.read_text(encoding="utf-8").splitlines() if s.strip()]
                    if argv_log.exists() else [])
            return p.returncode, rows

        def subject(row: list):
            if "--пуш" not in row:
                return None
            at = row.index("--пуш")
            return row[at + 1:at + 4]

        def push_action(row: list) -> bool:
            return "--действие" in row and row[row.index("--действие") + 1:][:1] == ["push"]

        for name, cmd, cwd, want_repo in (
                ("13а) git push origin <sha>:refs/heads/master (repo = cwd)", f"git push origin {ref}", repo, repo),
                ("13б) git -C <абсолютный> push …", f"git -C {repo} push origin {ref}", elsewhere, repo),
                ("13в) git -C <относительный симлинк> push … (repo = realpath)",
                 f"git -C link push origin {ref}", root, repo)):
            paths += 1
            rc, rows = gate_calls(cmd, cwd)
            got = [(push_action(r), subject(r)) for r in rows]
            want = [(True, ["origin", ref, str(want_repo)])]
            if got == want:
                print(f"  ок    {name}")
            else:
                bad += 1
                print(f"  ПЛОХО {name}: ждали зов ворот {want}, вышло {got} (rc сторожа {rc})")

        for name, cmd in (
                ("13г) охранный: git commit && git push … (Н-1)", f"git commit -qm x && git push origin {ref}"),
                ("13д) охранный: cd /d && git push …", f"cd {repo} && git push origin {ref}"),
                ("13е) охранный: X=1 git push …", f"X=1 git push origin {ref}"),
                ("13ж) охранный: GIT_DIR=/o git push …", f"GIT_DIR=/o git push origin {ref}"),
                ("13з) охранный: git -c k=v push …", f"git -c k=v push origin {ref}"),
                ("13и) охранный: timeout 5 git push …", f"timeout 5 git push origin {ref}"),
                ("13к) охранный: git push origin master", "git push origin master"),
                ("13л) охранный: git push", "git push"),
                ("13м) охранный: git push --force origin <sha>:…", f"git push --force origin {ref}"),
                ("13н) охранный: git push origin +<sha>:…", f"git push origin +{ref}"),
                ("13о) охранный: два пуша в одной команде", f"git push origin {ref}; git push origin {ref}")):
            paths += 1
            rc, rows = gate_calls(cmd, repo)
            with_subject = [r for r in rows if "--пуш" in r]
            # Ворота зовутся и здесь — текстовая дверь как прежде; только без предмета.
            if rows and all(push_action(r) for r in rows) and not with_subject:
                print(f"  ок    {name}")
            else:
                bad += 1
                print(f"  ПЛОХО {name}: зовы ворот {rows} (rc сторожа {rc})")
    return bad, paths


def main() -> int:
    bad = 0
    total = 0
    # Тест гоняет боевого сторожа сотни раз, и часть путей — БЕЗ конфига, то
    # есть с боевым LOG_DIR по умолчанию. Без этой переменной прогон засорял
    # журнал отмен: 137 записей за час, из них ~120 наделал сам тест, и метрика
    # «сколько раз сторож помешал работе» врала в разы (10.09.2026).
    # Проба следа ниже снимает переменную у себя — ей запись как раз нужна.
    os.environ["HARNESS_GUARD_NOLOG"] = "1"

    with tempfile.TemporaryDirectory() as tmp:
        env_чисто = среда_без_ротации(tmp)
        занято = живая_занятость()
        for want, name, cmd in CASES:
            total += 1
            got = run(cmd, env=env_чисто)
            # «когда другого нет» — условие о МАШИНЕ: при живом чужом прогоне
            # сторож обязан запретить второй, и путь непроверяем (улика 18.08).
            if got != want and занято and "когда другого нет" in name:
                print(f"  ПРОПУЩЕН ждали={want} получили={got}  {name} — машина занята")
                continue
            ok = "ок " if got == want else "ПЛОХО"
            if got != want:
                bad += 1
            print(f"  {ok} ждали={want} получили={got}  {name}")

    for want, name, событие, возраст in ROTATION_CASES:
        total += 1
        with tempfile.TemporaryDirectory() as tmp:
            got = run('tmux send-keys -t agent -l -- "/exit"',
                      env=среда_с_ротацией(tmp, событие, возраст))
        ok = "ок " if got == want else "ПЛОХО"
        if got != want:
            bad += 1
        print(f"  {ok} ждали={want} получили={got}  {name}")

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["HARNESS_INSTALL_CONF"] = str(pathlib.Path(tmp, "конфига-нет.conf"))
        for want, name, cmd in NOCONF_CASES:
            total += 1
            got = run(cmd, env=env)
            ok = "ок " if got == want else "ПЛОХО"
            if got != want:
                bad += 1
            print(f"  {ok} ждали={want} получили={got}  {name}")

    # Правила учёта — ДАННЫЕ установки, и это надо сказать вслух: без них
    # запрета нет вовсе (fail-open). Иначе «сторож держит живой учёт» читалось
    # бы как свойство кода, а код без данных не судит ничего.
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["HARNESS_PRODUCT_CONF"] = str(pathlib.Path(tmp, "данных-нет.conf"))
        env["HARNESS_PRODUCT_CONF_STEND"] = "1"
        total += 1
        got = run("curl -X PUT https://api.erp-obrazec.test/entity/product/7a1 -d '{}'",
                  env=env)
        if got != 0:
            print("  ПЛОХО  без данных учёта сторож выдумал запрет "
                  f"(код {got}) — у другой установки учёта нет вовсе"); bad += 1
        else:
            print("  ок  БОЛЬНОЙ СЛУЧАЙ: данных учёта нет — правил учёта нет (fail-open)")

    with tempfile.TemporaryDirectory() as tmp:
        envs = {True: gate_env(tmp + "/busy", True), False: gate_env(tmp + "/free", False)}
        for want, name, cmd, busy in GATE_CASES:
            total += 1
            got = run(cmd, env=envs[busy])
            ok = "ок " if got == want else "ПЛОХО"
            if got != want:
                bad += 1
            print(f"  {ok} ждали={want} получили={got}  {name}")

    # не-Bash и битый вход не должны мешать
    total += 1
    if run("что угодно", tool="Read") != 0:
        print("  ПЛОХО  не-Bash вызов должен проходить"); bad += 1
    else:
        print("  ок  не-Bash вызов проходит")
    # В-4: и этому подтесту — временный LOG_DIR, иначе след fail-open уходил
    # в боевой /var/log/harness/guard_exceptions.log прямо из теста.
    total += 1
    with tempfile.TemporaryDirectory() as tmp:
        conf = pathlib.Path(tmp, "install.conf")
        conf.write_text(f'LOG_DIR="{tmp}/log"\n', encoding="utf-8")
        env = dict(os.environ)
        env["HARNESS_INSTALL_CONF"] = str(conf)
        p = subprocess.run(GUARD, input="не json", capture_output=True, text=True, env=env)
    if p.returncode != 0:
        print("  ПЛОХО  битый вход должен проходить"); bad += 1
    else:
        print("  ок  битый вход проходит")

    # fail-open обязан оставлять след: битый JSON → команда пропущена И строка
    # в $LOG_DIR/guard_exceptions.log (LOG_DIR подсунут временным конфигом)
    total += 1
    with tempfile.TemporaryDirectory() as tmp:
        conf = pathlib.Path(tmp, "install.conf")
        conf.write_text(f'LOG_DIR="{tmp}/log"\n', encoding="utf-8")
        env = dict(os.environ)
        env["HARNESS_INSTALL_CONF"] = str(conf)
        p = subprocess.run(GUARD, input="не json", capture_output=True, text=True, env=env)
        log = pathlib.Path(tmp, "log", "guard_exceptions.log")
        rec = None
        if log.is_file():
            try:
                rec = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
            except (ValueError, IndexError):
                rec = None
        if p.returncode == 0 and rec and rec.get("reason") == "битый JSON на stdin" \
                and "ts" in rec and "detail" in rec:
            print("  ок  битый вход: пропущен И след в guard_exceptions.log")
        else:
            print("  ПЛОХО  битый вход: нет следа в guard_exceptions.log или код != 0"); bad += 1

    # Запреты возможностей: читаются ТОЛЬКО у установленных. Больной случай
    # здесь главный — набор лежит на диске, но не поставлен, и его запреты
    # обязаны молчать: иначе один скопированный каталог менял бы правила
    # сторожа без всякой установки.
    total += 1
    with tempfile.TemporaryDirectory() as tmp:
        pack = pathlib.Path(tmp, "proj", "harness", "vozmozhnosti", "проба")
        pack.mkdir(parents=True)
        (pack / "запреты.list").write_text(
            "# комментарий\n"
            "(?:^|[;&|(]\\s*)опасная-команда\\b\tэто запрет пробной возможности\n"
            "[\tсломанный регэксп — сторож обязан пропустить со следом\n",
            encoding="utf-8")
        conf = pathlib.Path(tmp, "install.conf")
        conf.write_text(f'PROJECT_DIR="{tmp}/proj"\nLOG_DIR="{tmp}/log"\n', encoding="utf-8")
        env = dict(os.environ)
        env["HARNESS_INSTALL_CONF"] = str(conf)
        не_поставлена = run("опасная-команда --сейчас", env=env)
        (pack / ".установлено").write_text('ID="проба"\n', encoding="utf-8")
        поставлена = run("опасная-команда --сейчас", env=env)
        чужая = run("безобидная-команда --сейчас", env=env)
        битый = run("сломанный регэксп в тексте", env=env)
        if не_поставлена == 0 and поставлена == 2 and чужая == 0 and битый == 0:
            print("  ок  запреты возможности: молчат до установки, действуют после, "
                  "битый регэксп не блокирует")
        else:
            print(f"  ПЛОХО  запреты возможности: не_поставлена={не_поставлена} "
                  f"поставлена={поставлена} чужая={чужая} битый={битый}")
            bad += 1

    # Запреты мобильной возможности — на её настоящем файле, а не на выдуманном:
    # правило проверяется тем текстом, который поедет на сервер.
    _MOBILE = next(
        (p for p in (_BASE.parent / "vozmozhnosti" / "mobilnaya-razrabotka",
                     _BASE.parent.parent / "harness" / "vozmozhnosti" / "mobilnaya-razrabotka")
         if (p / "запреты.list").is_file()), None)
    if _MOBILE:
        MOBILE_CASES = [
            (2, "Test Lab без --timeout — деньги владельца",
             "gcloud firebase test android run --type robo --app app.apk"),
            (0, "Test Lab С потолком проходит",
             "gcloud firebase test android run --type robo --app app.apk --timeout 5m"),
            (2, "ручной эмулятор мимо emu-start", "emulator -avd dev -no-window"),
            (0, "emu-start — разрешённый вход", "emu-start && emu-status"),
            (2, "выкладка в стор без подтверждения", "eas submit --platform android"),
            (0, "сборка релиза — не выкладка", "eas build --profile production --platform all"),
            (2, "лицензии SDK без yes — сессия повиснет", "sdkmanager --licenses"),
            (0, "лицензии SDK с yes проходят", "yes | sdkmanager --licenses"),
            (0, "ТЕКСТ про запрет — не команда",
             'echo "никогда не вызывай eas submit руками"'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            dst = pathlib.Path(tmp, "proj", "harness", "vozmozhnosti", "mobilnaya-razrabotka")
            dst.mkdir(parents=True)
            (dst / "запреты.list").write_text(
                (_MOBILE / "запреты.list").read_text(encoding="utf-8"), encoding="utf-8")
            (dst / ".установлено").write_text('ID="mobilnaya-razrabotka"\n', encoding="utf-8")
            conf = pathlib.Path(tmp, "install.conf")
            conf.write_text(f'PROJECT_DIR="{tmp}/proj"\nLOG_DIR="{tmp}/log"\n', encoding="utf-8")
            env = dict(os.environ)
            env["HARNESS_INSTALL_CONF"] = str(conf)
            for want, name, cmd in MOBILE_CASES:
                total += 1
                got = run(cmd, env=env)
                if got != want:
                    bad += 1
                print(f"  {'ок ' if got == want else 'ПЛОХО'} ждали={want} получили={got}"
                      f"  возможность: {name}")
    else:
        print("  ПЛОХО  запреты.list мобильной возможности не найден рядом — правила не проверены")
        bad += 1
        total += 1

    # В-3 живьём: при РАБОТАЮЩЕМ pytest настоящая команда блокируется,
    # а grep по слову pytest — нет (паттерн заякорен на позицию команды).
    fake = subprocess.Popen(["bash", "-c", "exec -a pytest sleep 60"])
    try:
        time.sleep(0.5)  # ps должен успеть увидеть процесс
        total += 1
        второй = ("setsid nohup python -m pytest tests/ -q "
                  "> /var/log/harness/x.log 2>&1 < /dev/null &")
        p2 = subprocess.run(
            GUARD,
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": второй}}),
            capture_output=True, text=True,
        )
        if p2.returncode == 2 and "уже идёт" in p2.stderr:
            print("  ок  живой pytest: настоящий второй прогон блокируется")
        else:
            print("  ПЛОХО  живой pytest: второй прогон должен блокироваться своей причиной,"
                  f" вышло код={p2.returncode}"); bad += 1
        total += 1
        if run("grep -rn pytest docs/") == 0:
            print("  ок  живой pytest: grep по слову pytest проходит")
        else:
            print("  ПЛОХО  живой pytest: grep по слову pytest не должен блокироваться"); bad += 1
    finally:
        fake.terminate()
        fake.wait()

    bad += проверить_прогон_в_сессии()
    _плохо_т, _путей_т = проверить_гнездо_tmux()
    bad += _плохо_т
    total += _путей_т
    _плохо_у, _путей_у = проверить_гнездо_по_uid()
    bad += _плохо_у
    total += _путей_у
    _плохо_п, _путей_п = проверить_подсказку_pgrep()
    bad += _плохо_п
    total += _путей_п
    _плохо_с, _путей_с = проверить_совет_проходит()
    bad += _плохо_с
    total += _путей_с
    _плохо_н, _путей_н = проверить_подсказку_носителя()
    bad += _плохо_н
    total += _путей_н
    _плохо, _путей = проверить_публикацию_мимо_двери()
    bad += _плохо
    total += _путей
    _плохо_сеть, _путей_сеть = проверить_сеть_мимо_двери()
    bad += _плохо_сеть
    total += _путей_сеть
    _плохо_г, _путей_г = проверить_гашение_по_выборке()
    bad += _плохо_г
    total += _путей_г
    _плохо_д, _путей_д = проверить_дверь_previous()
    _плохо_дан, _путей_дан = проверить_дверь_dannyh()
    bad += _плохо_д
    total += _путей_д
    bad += _плохо_дан
    total += _путей_дан
    for check_holes in (check_protected_paths, check_delete_door_targets,
                        check_consent_log_writes):
        holes_bad, holes_paths = check_holes()
        bad += holes_bad
        total += holes_paths
    total += len(ФОНОВЫЕ_СЛУЧАИ)

    плохо_пуш, путей_пуш = check_push_subject_hook()
    bad += плохо_пуш
    total += путей_пуш
    bad += прогоны_ворот()
    total += ЧИСЛО_ПУТЕЙ_ВОРОТ

    print(f"\nвсего путей: {total}, неудач: {bad}")
    return 1 if bad else 0


# ── юнит-прогоны настоящих ворот deploy_guard.py (Б-1, В-7, К-3) ────────────
ЧИСЛО_ПУТЕЙ_ВОРОТ = 13


def dg_scenario(tmp: str, autonomy: str = "full", кнопка_минут_назад: int | None = None,
                tmux: str = "agent", слово: str | None = None) -> tuple[dict, pathlib.Path]:
    """Временные install.conf + harness.conf + $LOG_DIR/locks/services для ворот.

    Дерево выката — свой чистый git-репозиторий (PROJECT_DIR): предмет кнопки считается по нему.
    `кнопка_минут_назад` кладёт «Да» кнопкой на предмет этого дерева, `слово` — текст владельца
    во входящие: он дверь не открывает. Каталог слова и ключи согласия судья берёт у носителя:
    ворота стенда идут обёрткой (run_dg), носитель — из этих же install.conf и harness.conf.
    """
    log = pathlib.Path(tmp, "log")
    (log / "locks" / "services").mkdir(parents=True, exist_ok=True)
    проект = pathlib.Path(tmp, "proj")
    проект.mkdir(exist_ok=True)
    git_env = dict(os.environ, GIT_AUTHOR_NAME="проба", GIT_AUTHOR_EMAIL="p@stand.invalid",
                   GIT_COMMITTER_NAME="проба", GIT_COMMITTER_EMAIL="p@stand.invalid",
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    (проект / "a.txt").write_text("a\n", encoding="utf-8")
    for команда in (["init", "-q", "-b", "master"], ["add", "-A"], ["commit", "-q", "-m", "проба"]):
        subprocess.run(["git", "-C", str(проект), *команда], check=True, capture_output=True, env=git_env)
    pathlib.Path(tmp, "install.conf").write_text(
        f'LOG_DIR="{log}"\nAUTONOMY="{autonomy}"\nPROJECT_DIR="{проект}"\n' + ПАСПОРТ_ВЛАДЕЛЬЦА,
        encoding="utf-8")
    pathlib.Path(tmp, "harness.conf").write_text(
        'SERVICE_LOCKS_SUBDIR="services"\nCONFIRM_MAX_AGE_MIN=60\nDEPLOY_NEEDS_OWNER=1\n'
        f'DEPLOY_CONSENT_STANDING=0\nCONSENT_BUTTON_TTL_MIN=15\nTMUX_SESSION="{tmux}"\n', encoding="utf-8")
    когда = lambda минут: (datetime.datetime.now().astimezone()
                           - datetime.timedelta(minutes=минут)).isoformat(timespec="seconds")
    (log / "inbox.jsonl").write_text(
        json.dumps({"ts": когда(5), "text": слово, "message_id": "1"}, ensure_ascii=False) + "\n"
        if слово is not None else "", encoding="utf-8")
    if кнопка_минут_назад is not None:
        sha = subprocess.run(["git", "-C", str(проект), "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
        данные = {"nonce": "n", "вид": "выкат", "цель": f"{sha} {os.path.realpath(проект)}",
                  "message_id": 4000, "from_id": ВЛАДЕЛЕЦ_СТЕНДА}
        нажатие = json.dumps({"v": 1, "ts": когда(кнопка_минут_назад), "src": "проба", "event": "consent.yes",
                              "outcome": "ok", "data": данные}, ensure_ascii=False) + "\n"
        (log / "consent-buttons.jsonl").write_text(нажатие, encoding="utf-8")
        (log / "consent-requests.jsonl").write_text(_просьба_к(нажатие), encoding="utf-8")
    env = dict(os.environ)
    env.pop("HARNESS_LOG_DIR", None)      # дверь запускающего перебила бы LOG_DIR стенда
    env["HARNESS_INSTALL_CONF"] = str(pathlib.Path(tmp, "install.conf"))
    env["HARNESS_CONF"] = str(pathlib.Path(tmp, "harness.conf"))
    return env, log


def run_dg(env: dict, *args: str) -> tuple[int, str]:
    """Настоящие ворота main() в обёртке: судье — носитель стенда dg_scenario."""
    p = subprocess.run(зов_обёртки(DEPLOY_GUARD, env, *args),
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def живая_занятость() -> str:
    """Что мешает воротам сказать «можно» прямо сейчас: долгий pytest соседней
    сессии, идущий выкат. Это состояние МАШИНЫ, а не логики ворот.

    Улика 18.08.2026: при идущем полном прогоне тестов семь путей из 135 стали
    красными, и две сессии подряд разбирали, дефект это И-2 или нет (дефекта не
    было). Гейт, краснеющий от постороннего процесса, перестают читать —
    поэтому такие пути теперь ПРОПУСКАЮТСЯ с названной причиной, а не врут.
    """
    with tempfile.TemporaryDirectory() as t:
        своё, _ = dg_scenario(t)                  # чистое окружение: локов нет
        rc, out = run_dg(своё)
    # Обёртка без Носитель — не занятость машины: пути обязаны краснеть, а не пропускаться.
    if rc in (0, КОД_ОБЁРТКИ):
        return ""
    мешает = [с for с in out.splitlines() if с.startswith(("занято:", "процесс "))]
    return "; ".join(мешает)[:120] or out.strip()[:120]


def прогоны_ворот() -> int:
    """Возвращает число неудач; путей ровно ЧИСЛО_ПУТЕЙ_ВОРОТ."""
    bad = 0
    мешает = ""

    def итог(имя: str, ок: bool, детали: str = "", ждёт_свободы: bool = False) -> None:
        nonlocal bad
        if ок:
            print(f"  ок  ворота: {имя}")
        elif ждёт_свободы and мешает:
            print(f"  ПРОПУЩЕН  ворота: {имя} — машина занята ({мешает})")
        else:
            print(f"  ПЛОХО  ворота: {имя}  [{детали[:160]}]")
            bad += 1

    if DEPLOY_GUARD is None:
        print("  ПЛОХО  ворота: deploy_guard.py не найден рядом с тестом")
        return ЧИСЛО_ПУТЕЙ_ВОРОТ

    мешает = живая_занятость()
    if мешает:
        print(f"  (машина занята: {мешает} — пути, ждущие «можно», будут пропущены)")

    держатель = subprocess.Popen(["sleep", "60"])  # живой не-предок для локов
    try:
        # 1. свободно
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t)
            rc, out = run_dg(env)
            итог("свободно → можно", rc == 0, out, ждёт_свободы=True)
        # 2. Б-1: сервисный лок в locks/services/ с живым PID игнорируется
        with tempfile.TemporaryDirectory() as t:
            env, log = dg_scenario(t)
            (log / "locks" / "services" / "tg-dispatcher.lock").write_text(
                f"{держатель.pid}\ntg-dispatcher, живой сервис\n", encoding="utf-8")
            rc, out = run_dg(env)
            итог("Б-1: сервисный лок в services/ не блокирует",
                 rc == 0 and "tg-dispatcher.lock" not in out, out, ждёт_свободы=True)
        # 3. операционный лок с живым чужим PID блокирует
        with tempfile.TemporaryDirectory() as t:
            env, log = dg_scenario(t)
            (log / "locks" / "job.lock").write_text(
                f"{держатель.pid}\nдолгая работа\n", encoding="utf-8")
            rc, out = run_dg(env)
            итог("операционный лок блокирует", rc == 1 and "job.lock" in out, out)
        # 4. протухший лок (мёртвый PID) не блокирует
        мертвец = subprocess.Popen(["sleep", "0"]); мертвец.wait()
        with tempfile.TemporaryDirectory() as t:
            env, log = dg_scenario(t)
            (log / "locks" / "old.lock").write_text(
                f"{мертвец.pid}\nбыла работа\n", encoding="utf-8")
            rc, out = run_dg(env)
            итог("протухший лок не блокирует", rc == 0 and "протухший" in out, out, ждёт_свободы=True)
        # 5. К-3: лок собственного предка (deploy.sh берёт лок до ворот) не блокирует
        with tempfile.TemporaryDirectory() as t:
            env, log = dg_scenario(t)
            (log / "locks" / "deploy.lock").write_text(
                f"{os.getpid()}\nвыкат deploy.sh (это мы сами)\n", encoding="utf-8")
            rc, out = run_dg(env)
            итог("К-3: свой лок (предок) не блокирует", rc == 0 and "сам вызвавший" in out, out,
                 ждёт_свободы=True)
        # 6. В-7: semi без согласия → занято
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi")
            rc, out = run_dg(env)
            итог("В-7: semi без согласия → занято", rc == 1 and "нет согласия владельца" in out, out)
        # 7. В-7: semi со свежим «Да» кнопкой на предмет → можно
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi", кнопка_минут_назад=5)
            rc, out = run_dg(env)
            итог("В-7: свежее «Да» кнопкой → можно", rc == 0 and "согласие кнопкой" in out, out,
                 ждёт_свободы=True)
        # 8. В-7: старое «Да» кнопкой (2 часа, срок 15 мин) → занято
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi", кнопка_минут_назад=120)
            rc, out = run_dg(env)
            итог("В-7: старое «Да» → занято", rc == 1 and "нет согласия владельца" in out, out)
        # 9. В-7: текст владельца «выкатывай» кнопки не заменяет — выкат закрыт, отказ называет кнопку
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi", слово="выкатывай")
            rc, out = run_dg(env)
            итог("В-7: текст «выкатывай» выкат не открывает", rc == 1 and "кнопк" in out, out)
        # 10. В-7: AUTONOMY=full — проверка пропущена с честной строкой
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="full")
            rc, out = run_dg(env)
            итог("В-7: full — пропуск с честной строкой", rc == 0 and "не проверяется" in out, out,
                 ждёт_свободы=True)
        # 11. В-7: --силой снимает только занятость; без «да» — отказ.
        # Согласие судится ДО занятости, поэтому путь от машины не зависит.
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi")
            rc, out = run_dg(env, "--силой")
            итог("В-7: --силой без кнопки — отказ, --силой не снимает отсутствие согласия",
                 rc == 1 and "нет согласия владельца" in out, out)
        # 12-13. Б-1: интерактивный claude в tmux-сессии агента — не занятость
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, tmux="нет-такой-сессии-731")
            rc, out = run_dg(env)
            # Ожидание — честная строка и живые ворота, а НЕ конкретный код.
            # Улика 11.08.2026: путь ждал rc==0 и краснел при запуске из
            # systemd. Причина не в воротах: когда tmux недоступен,
            # интерактивный claude агента не исключается — и это ровно то
            # поведение, которое путь проверяет. Из сессии агента тот же
            # claude отсеивался как предок, и rc был 0. Ожидание, зависящее
            # от родословной запускающего, — недетерминированный тест.
            итог("Б-1: tmux недоступен → честная строка, ворота живы",
                 rc in (0, 1) and "недоступна" in out and "Traceback" not in out, out)
        import importlib.util
        spec = importlib.util.spec_from_file_location("dg_под_тестом", DEPLOY_GUARD)
        dg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dg)
        исходный = dg._tty_процесса
        try:
            dg._tty_процесса = lambda pid: "/dev/pts/9"
            в_сессии = dg._процесс_агента("1", {"/dev/pts/9"})
            другой_tty = dg._процесс_агента("1", {"/dev/pts/2"})
            без_tmux = dg._процесс_агента("1", None)
        finally:
            dg._tty_процесса = исходный
        итог("Б-1: claude на tty tmux-сессии исключён; чужой tty и без tmux — нет",
             в_сессии is True and другой_tty is False and без_tmux is False,
             f"в_сессии={в_сессии}, другой_tty={другой_tty}, без_tmux={без_tmux}")
        # ── след отмены: он живёт вне репозитория, и в него уезжает командная
        # строка. БОЛЬНОЙ СЛУЧАЙ 10.09.2026 — пароль боевой базы уже утекал в
        # логи 257 раз через рецепты make; журнал отмен не должен повторить это.
        import importlib.util as _iu
        _spec = _iu.spec_from_file_location("guard_под_тестом", GUARD[1])
        _g = _iu.module_from_spec(_spec)
        _spec.loader.exec_module(_g)
        с_dsn = _g.без_тайн("psql postgresql://app:СуперПароль@localhost:5434/db")
        с_ключом = _g.без_тайн("PGPASSWORD=СуперПароль psql -h localhost")
        обычная = _g.без_тайн("docker compose restart api")
        итог("след отмены: пароль в DSN замаскирован",
             "СуперПароль" not in с_dsn and "app:***@" in с_dsn, с_dsn)
        итог("след отмены: значение PGPASSWORD замаскировано",
             "СуперПароль" not in с_ключом and "PGPASSWORD=***" in с_ключом, с_ключом)
        итог("след отмены: обычная команда не искажается",
             обычная == "docker compose restart api", обычная)

        # Сама запись следа: отмена обязана оставлять строку JSONL, иначе
        # ревизия снова упрётся в «доказательство только в памяти агента».
        # Свой временный LOG_DIR: боевой журнал отмен проверка засорять не должна.
        import json as _json
        with tempfile.TemporaryDirectory() as _tmp:
            _conf = pathlib.Path(_tmp, "install.conf")
            _conf.write_text(f'LOG_DIR="{_tmp}"\nAUTONOMY="semi"\n', encoding="utf-8")
            _env = dict(os.environ, HARNESS_INSTALL_CONF=str(_conf))
            _env.pop("HARNESS_GUARD_NOLOG", None)   # эта проба ЖДЁТ записи следа
            _p = subprocess.run(GUARD, input=_json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "docker compose restart api"}}),
                capture_output=True, text=True, env=_env)
            след = pathlib.Path(_tmp, "guard.jsonl")
            строки = [_json.loads(l) for l in след.read_text(encoding="utf-8").splitlines()
                      if l.strip()] if след.exists() else []
            итог("отмена оставляет след в guard.jsonl (rule, argv0, cmd_head)",
                 _p.returncode == 2 and len(строки) == 1
                 and строки[0].get("verdict") == "deny"
                 and строки[0].get("argv0") == "docker"
                 and "restart" in строки[0].get("cmd_head", ""),
                 f"rc={_p.returncode}, строк={len(строки)}: {строки[:1]}")
    finally:
        держатель.terminate()
        держатель.wait()
    return bad


if __name__ == "__main__":
    # Одна проба 13 отдельно: весь прогон сторожа долог для приёмки одной задачи.
    if sys.argv[1:] == ["--случай", "13"]:
        плохо, путей = check_push_subject_hook()
        print(f"падений {плохо} из {путей} путей")
        sys.exit(1 if плохо else 0)
    sys.exit(main())
