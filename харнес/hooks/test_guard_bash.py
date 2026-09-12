#!/usr/bin/env python3
# Взят из UNIFIED/templates/hooks/test_guard_bash.py; добавлены пары красный/зелёный на И-1
# (drop database, rm -r по данным), И-2 (docker cp в контейнер) и прогон ворот deploy_guard.py
# через временный конфиг (HARNESS_INSTALL_CONF).
"""Прогон хука-сторожа по всем путям.

Отдельным файлом, а не строкой в оболочке: сторож разбирает текст команды,
и тестовые примеры внутри командной строки он видит как саму команду.

Запускается без /etc/harness/install.conf — это тоже проверка: сторож обязан
работать с дефолтами и fail-open на свежей машине без конфига.
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

# Путь относительно самого теста: набор переносится целиком, тест работает из любого каталога.
# Путь к сторожу подменяем переменной окружения — так мутационная проба
# (scripts/mutacionnaya-proba.sh) гоняет боевой тест против ИСПОРЧЕННОЙ копии и
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
    (0, "обычный headless-запуск с MCP — разрешён", 'claude -p "сделай X"'),
    (0, "он же в конвейере", 'echo hi | claude -p "x"'),
    (2, "запуск С ТЕЛЕГРАМ-КАНАЛОМ — отберёт getUpdates", 'claude --channels plugin:telegram@claude-plugins-official'),
    (2, "телеграм в наборе MCP", 'claude -p "x" --mcp-config \'{"mcpServers":{"telegram":{"command":"npx"}}}\' --strict-mcp-config'),
    (0, "он же в подстановке", 'out=$(claude -p "x")'),
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
    (0, "запуск с пустым набором MCP", "claude -p \"x\" --mcp-config '{\"mcpServers\":{}}' --strict-mcp-config"),
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
    (0, "законный уход со сменой — через сторожа", "харнес/demons/session-warden.sh --rotate-now"),
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
    (0, "В-2: timeout перед обычным claude", "timeout 60 claude -p x"),
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
    (0, "pgrep без -f смотрит имя процесса, а не командную строку", "pgrep pytest"),
    (0, "шаблон из переменной — раскрытия нет, самосовпадения не доказать", 'pgrep -f "$ИМЯ"'),
    (0, "В-3: pgrep в тексте документации — не команда", "grep -rn 'pgrep -f' docs/"),
    (0, "В-3: echo про pgrep — не команда", "echo 'не жди через pgrep -f progon_dvizhka.py'"),
]

# Случаи БЕЗ install.conf: (ожидаемый код, описание, команда). Гоняются с
# HARNESS_INSTALL_CONF на заведомо несуществующий файл. Раньше лежали в CASES,
# где окружение наследуется целиком, — и на сервере с установленным
# /etc/harness/install.conf «без конфига» означало «с боевым конфигом»:
# ворота честно отвечали «занято» (semi без свежего согласия), тест краснел.
# Улика приёмки 11.08.2026: проверка обязана СОЗДАВАТЬ условие, а не надеяться
# на состояние машины.
NOCONF_CASES = [
    (0, "без install.conf ворота молчат: git push проходит", "git push"),
    (0, "без install.conf deploy.sh не блокируется", "./scripts/deploy.sh"),
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


def run_процессом(cmd: str, tool: str = "Bash", env: dict | None = None) -> int:
    """Настоящий запуск: проверяет контракт процесса целиком."""
    p = subprocess.run(
        GUARD,
        input=json.dumps({"tool_name": tool, "tool_input": {"command": cmd}}),
        capture_output=True, text=True, env=env,
    )
    return p.returncode


def run(cmd: str, tool: str = "Bash", env: dict | None = None) -> int:
    """Тот же вердикт, но без запуска процесса.

    221 путь × 107 мс отдельного python = 24 с из 41 с всех ворот (замер
    11.09.2026). Окружение сторож читает ВНУТРИ функций и конфиг не кэширует —
    подмена os.environ здесь равносильна env= у subprocess (проверено тем, что
    пути про ворота выката и ротацию остались зелёными).
    Не-Bash вызовы отсекает main(), а не check(), — их гоняем процессом.
    """
    if tool != "Bash":
        return run_процессом(cmd, tool, env)
    прежнее = dict(os.environ)
    прежняя_сессия = getattr(ГВАРД, "СЕССИЯ", "")
    try:
        if env is not None:
            os.environ.clear()
            os.environ.update(env)
        ГВАРД.СЕССИЯ = ""
        try:
            return ГВАРД.check(cmd)
        except SystemExit as e:          # сторож вправе выйти сам
            return int(e.code or 0)
        except Exception:
            # main() ловит исключения сторожа и пропускает команду (fail-open) —
            # внутрипроцессный вызов обязан вести себя так же, иначе тест
            # объявил бы красным то, что в бою зелено.
            return 0
    finally:
        os.environ.clear()
        os.environ.update(прежнее)
        ГВАРД.СЕССИЯ = прежняя_сессия


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
        pack = pathlib.Path(tmp, "proj", "харнес", "возможности", "проба")
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
        (p for p in (_BASE.parent / "возможности" / "мобильная-разработка",
                     _BASE.parent.parent / "харнес" / "возможности" / "мобильная-разработка")
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
            dst = pathlib.Path(tmp, "proj", "харнес", "возможности", "мобильная-разработка")
            dst.mkdir(parents=True)
            (dst / "запреты.list").write_text(
                (_MOBILE / "запреты.list").read_text(encoding="utf-8"), encoding="utf-8")
            (dst / ".установлено").write_text('ID="мобильная-разработка"\n', encoding="utf-8")
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
    total += len(ФОНОВЫЕ_СЛУЧАИ)

    bad += прогоны_ворот()
    total += ЧИСЛО_ПУТЕЙ_ВОРОТ

    print(f"\nвсего путей: {total}, неудач: {bad}")
    return 1 if bad else 0


# ── юнит-прогоны настоящих ворот deploy_guard.py (Б-1, В-7, К-3) ────────────
ЧИСЛО_ПУТЕЙ_ВОРОТ = 13


def dg_scenario(tmp: str, autonomy: str = "full", confirm: str | None = None,
                tmux: str = "agent") -> tuple[dict, pathlib.Path]:
    """Временные install.conf + harness.conf + $LOG_DIR/locks/services для ворот."""
    log = pathlib.Path(tmp, "log")
    (log / "locks" / "services").mkdir(parents=True, exist_ok=True)
    pathlib.Path(tmp, "install.conf").write_text(
        f'LOG_DIR="{log}"\nAUTONOMY="{autonomy}"\n', encoding="utf-8")
    pathlib.Path(tmp, "harness.conf").write_text(
        'SERVICE_LOCKS_SUBDIR="services"\nCONFIRM_MAX_AGE_MIN=60\n'
        f'TMUX_SESSION="{tmux}"\n', encoding="utf-8")
    if confirm is not None:
        (log / "confirmations.jsonl").write_text(confirm, encoding="utf-8")
        # Согласие БЕЗ названного предмета («да», «ок») с 11.09.2026 годится
        # только действию, о котором спрашивали: ворота запоминают его при
        # отказе. В жизни отказ всегда предшествует ответу владельца, здесь
        # он смоделирован — иначе проверка ставила бы условие, которого в
        # работе не бывает.
        (log / "согласие.json").write_text(
            json.dumps({"ожидание": {"действие": "deploy", "ts": time.time()}}),
            encoding="utf-8")
    env = dict(os.environ)
    env["HARNESS_INSTALL_CONF"] = str(pathlib.Path(tmp, "install.conf"))
    env["HARNESS_CONF"] = str(pathlib.Path(tmp, "harness.conf"))
    return env, log


def run_dg(env: dict, *args: str) -> tuple[int, str]:
    p = subprocess.run(["python3", str(DEPLOY_GUARD), *args],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def _подтверждение(текст: str, минут_назад: int = 0) -> str:
    ts = (datetime.datetime.now().astimezone()
          - datetime.timedelta(minutes=минут_назад)).isoformat()
    return json.dumps({"ts": ts, "text": текст, "message_id": "1"},
                      ensure_ascii=False) + "\n"


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
    if rc == 0:
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
        # 7. В-7: semi со свежим «да» → можно
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi", confirm=_подтверждение("да", 5))
            rc, out = run_dg(env)
            итог("В-7: свежее «да» → можно", rc == 0 and "согласие владельца" in out, out,
                 ждёт_свободы=True)
        # 8. В-7: старое «да» (2 часа) → занято
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi", confirm=_подтверждение("да", 120))
            rc, out = run_dg(env)
            итог("В-7: старое «да» → занято", rc == 1 and "нет согласия владельца" in out, out)
        # 9. В-7: свежее «ротация ок» — согласие на РОТАЦИЮ, не на выкат
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi", confirm=_подтверждение("ротация ок", 5))
            rc, out = run_dg(env)
            итог("В-7: «ротация ок» выкат не разрешает", rc == 1, out)
        # 10. В-7: AUTONOMY=full — проверка пропущена с честной строкой
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="full")
            rc, out = run_dg(env)
            итог("В-7: full — пропуск с честной строкой", rc == 0 and "не проверяется" in out, out,
                 ждёт_свободы=True)
        # 11. В-7: --силой печатает отсутствие согласия отдельной строкой жертв
        with tempfile.TemporaryDirectory() as t:
            env, _ = dg_scenario(t, autonomy="semi")
            rc, out = run_dg(env, "--силой")
            итог("В-7: --силой называет «нет согласия» жертвой",
                 rc == 0 and "нет согласия владельца" in out, out, ждёт_свободы=True)
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
    sys.exit(main())
