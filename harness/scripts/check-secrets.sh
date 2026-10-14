#!/usr/bin/env bash
#
# check-secrets.sh — секреты не попадают в git.
#
# Откуда взят: UNIFIED/templates/check-secrets.sh (проверка .env на дефолты
# перед выкатом). Что изменено: назначение переработано под гейт коммита —
# на чистом сервере ловить надо не «changeme в .env» (это пункт приёмки при
# установке, 01-SPEC), а токен, случайно закоммиченный в репозиторий; поэтому
# здесь прогон по учтённым в git файлам с паттернами токенов/паролей.
# Проверка дефолтов из шаблона сознательно не тащится: у неё другой момент
# жизни (установка, не коммит) и другой владелец (00-ВВОДНЫЕ §5).
#
# Прогон СТРОГО по git ls-files, не обходом каталога: обход втянул бы
# node_modules и venv, где «секрет» найдётся на любой вкус, и гейт умер бы
# от ложных срабатываний (та же улика, что в fix-claude-mem-project.py).
#
# Ложное срабатывание гасится маркером в той же строке:  # не-секрет
#
# Запуск:
#   check-secrets.sh [каталог]     # все учтённые файлы (по умолчанию $PROJECT_DIR)
#   check-secrets.sh --staged      # только staged-содержимое (для pre-commit)
#   check-secrets.sh --selftest    # больной + здоровый случай во временном репо
#
# Код возврата: 0 — чисто, 1 — найден похожий на секрет текст, 2 — нет репо.
set -euo pipefail

# Паттерны — только то, что похоже на настоящий секрет, а не на слово «пароль»:
#   присвоение секрета строкой от 6 знаков — В КАВЫЧКАХ и БЕЗ. Безкавычечный  # не-секрет
#   паттерн (password = supersecret123, .env-стиль) строже кавычечного,  # не-секрет
#   иначе гейт умирает от ложных срабатываний: значение обязано дойти до
#   пробела/конца строки, содержать цифру (пароль без единой цифры он
#   пропустит — названная цена) и не содержать скобок (это вызовы функций,
#   не секреты); ведущие - и + отсекают шелловские ${VAR:-дефолт}.
#   Значения, начинающиеся с $ / { < — пути и подстановки, не секреты:
#   TG_TOKEN_FILE="/etc/.../token" — законно;
#   токены Telegram/AWS/Google/GitHub/Slack по их фиксированным форматам;
#   приватные ключи по заголовку PEM. Регистронезависимо, KEY= и key= равны.
PATTERNS=(
    -e '(token|secret|passwd|password|api_?key)[A-Za-z0-9_]*[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"'$/{<[:space:]][^"'"'"'[:space:]]{5,}'
    -e '(token|secret|passwd|password|api_?key)[A-Za-z0-9_]*[[:space:]]*[:=][[:space:]]*[^"'"'"'$/{<+([:space:]-][^()[:space:]]{3,}[0-9][^()[:space:]]*([[:space:]]|$)'
    -e '[0-9]{8,10}:AA[0-9A-Za-z_-]{33}'
    -e 'AKIA[0-9A-Z]{16}'
    -e 'AIza[0-9A-Za-z_-]{35}'
    -e 'ghp_[A-Za-z0-9]{36}'
    -e 'github_pat_[A-Za-z0-9_]{20,}'
    -e 'xox[baprs]-[0-9A-Za-z-]{10,}'
    -e '-----BEGIN [A-Z ]*PRIVATE KEY-----'
    # Секрет БЕЗ говорящего имени: ключ подписи, внутренний ключ и пропуска
    # панели порождает secrets.token_urlsafe(32) — ровно 43 знака. 13.09.2026
    # ревью безопасности нашло живой пропуск панели в коде гейта под именем
    # «ПРОПУСК»: кириллическое имя мимо шаблонов выше, и сторож молчал.
    # Замер по всему репозиторию перед включением: находок 0.
    -e '=[[:space:]]*["'"'"'][A-Za-z0-9_-]{43}["'"'"']'
)
MARKER='не-секрет'
# Номер версии — не секрет. Секрет узнаётся по ФОРМЕ значения
# ([[a-secret-is-known-by-its-shape]]), а «js-tokens: 9.0.1» в pnpm-lock —  # не-секрет
# это ИМЯ ПАКЕТА со словом token и версия следом. Живой случай 29.09.2026:
# гейт краснел на каждом коммите, меняющем lock-файл, а пометить строку
# маркером нельзя — pnpm стирает комментарии при перегенерации.
# Отсев узкий: значение целиком — цифры, точки и обычный суффикс версии.
VERSIYA='(token|secret|passwd|password|api_?key)[A-Za-z0-9_-]*[[:space:]]*[:=][[:space:]]*"?v?[0-9]+(\.[0-9]+)+([-+][0-9A-Za-z.]+)?"?[[:space:]]*$'

run_scan() {  # $1 = каталог репо, $2 = режим all|staged, $3 = каталог ключей (по умолчанию — из паспорта)
    local repo="$1" mode="$2" fail=0 files hits f
    # «Нечем судить» и «судить есть чем, а git сломался» — РАЗНЫЕ ответы, и
    # второй жёлтым быть не вправе: И-3 держится этим гейтом, а жёлтый пускает
    # коммит (находка ревью пайплайна M-2, тот же класс, что дыра 21.09).
    # Каталог .git есть — репозиторий существует, и молчание git это поломка.
    if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
        if [ -e "$repo/.git" ]; then
            echo "check-secrets: $repo — git не ответил, а репозиторий на месте: секреты НЕ проверены"
            return 1
        fi
        echo "check-secrets: $repo — не git-репозиторий, сканировать нечего (жёлтый)"
        return 77
    fi
    # core.quotepath=false: без него git экранирует кириллические имена
    # («память/» → \320...), и поиск по такому имени падает. Поймано
    # прогоном при сборке пакета 08.08.2026.
    #
    # ОДИН `git grep` на всё, а не `git show` + `grep` на каждый файл: прежний
    # обход стоил 10,6 с из 41 с всех ворот (замер 11.09.2026), потому что на
    # каждый из сотен учтённых файлов запускалось по два процесса. `git grep`
    # ищет по УЧТЁННЫМ файлам сам — обход каталога с node_modules и venv,
    # ради которого и заводился список файлов, ему не грозит.
    # --cached в обоих режимах: коммитится ИНДЕКС, его и читаем.
    local -a SCOPE=()
    if [ "$mode" = "staged" ]; then
        files=$(git -C "$repo" -c core.quotepath=false diff --cached --name-only --diff-filter=ACM)
        [ -z "$files" ] && { echo "check-secrets: файлов нет — чисто"; return 0; }
        # Ограничиваем область именно изменёнными файлами: остальное уже в репо
        # и этим коммитом не вносится.
        SCOPE=(--)
        while IFS= read -r f; do [ -n "$f" ] && SCOPE+=("$f"); done <<< "$files"
    fi
    # Замер 11.09.2026 на 1412 учтённых файлах (69 МБ): обход «git show + grep
    # на файл» — 10 609 мс, один `git grep -E` — 5 967 мс, он же с PCRE — 296 мс.
    # PCRE собран в git не везде (харнес ставится на чужие серверы), поэтому
    # поддержка проверяется одной командой, а не предполагается.
    # Проба по ЗАВЕДОМО ПУСТОЙ области: она отвечает за 25 мс, не читая ни
    # одного файла. Проба по «.» стоила бы 400 мс — столько же, сколько сам
    # гейт (замер 11.09.2026). Код 1 — «PCRE есть, совпадений нет»;
    # git без PCRE отвечает 12x «cannot use Perl-compatible regexes».
    # Код пробы читаем ТУТ ЖЕ в условии: отдельной строкой под `set -e` она
    # уронила бы весь гейт своим законным «ничего не нашлось» (код 1) — гейт
    # выходил 1 и не печатал ни строки, как будто нашёл секрет. Пойман сразу
    # замером: 21 мс и пустой вывод вместо «чисто».
    local RE_FLAG=-P RC_PROBE=0
    git -C "$repo" grep --cached -qP 'a' -- 'нет-такого-файла-*' >/dev/null 2>&1 || RC_PROBE=$?
    [ "$RC_PROBE" -le 1 ] || RE_FLAG=-E
    # grep-коды: 0 — нашлось, 1 — чисто, >1 — беда. «|| true» и разбор кода
    # отдельно: под set -e «ничего не нашлось» уронило бы весь гейт.
    hits=$(git -C "$repo" -c core.quotepath=false grep --cached -inI "$RE_FLAG" \
             "${PATTERNS[@]}" "${SCOPE[@]}" 2>/dev/null | grep -v "$MARKER" \
             | grep -vE "$VERSIYA" || true)
    if [ -n "$hits" ]; then
        printf '%s\n' "$hits" | sed 's|^|СЕКРЕТ? |'
        fail=1
    fi
    # ВТОРОЙ ВОПРОС ГЕЙТА: не попал ли в коммит НАШ живой ключ. Формой его не
    # поймать — сорок шестнадцатеричных знаков неотличимы от git-хэша, и шаблон
    # на такую форму красил бы передачи и журналы. Зато значение у нас есть:
    # оно лежит в $SECRETS_DIR, и сравнение с ним точное.
    #
    # Живой случай 29.09.2026: владелец прислал ключ учёта в канал, демон карты
    # скопировал сообщение в карточку ДОСЛОВНО — ключ оказался в dev-map.yaml,
    # а гейт сказал «чисто»: он искал говорящее имя рядом со значением, а тут
    # было голое число.
    #
    # Короткие значения не берём: имя бота, номер чата и прочие двузначные
    # строки дали бы совпадение на ровном месте. Шестнадцать знаков — граница,
    # ниже которой значение не является ключом ни у одного нашего носителя.
    local SECRETS_DIR_LOCAL VALUES_FILE HIT_FILES
    # Паспорт читается ОБЩИМ загрузчиком (его же зовёт выбор каталога репо), а
    # не своим sed: вторая разборщица конфига разошлась бы с первой молча.
    SECRETS_DIR_LOCAL="${3:-}"
    if [ -z "$SECRETS_DIR_LOCAL" ]; then
        # shellcheck disable=SC1091
        source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
        SECRETS_DIR_LOCAL="$(konf_iz_fajla SECRETS_DIR)"
    fi
    if [ -n "$SECRETS_DIR_LOCAL" ] && [ -d "$SECRETS_DIR_LOCAL" ]; then
        VALUES_FILE=$(mktemp)
        # Значения читаются, но НЕ печатаются: вывод гейта оседает в журналах и
        # в транскрипте сессии — напечатанный ключ и есть утечка.
        find "$SECRETS_DIR_LOCAL" -maxdepth 1 -type f -print0 2>/dev/null \
          | xargs -0 -r cat 2>/dev/null \
          | tr -d '\r' \
          | awk 'length($0) >= 16 && $0 !~ /[[:space:]"{}:,]/ { print }' \
          | sort -u > "$VALUES_FILE"
        if [ -s "$VALUES_FILE" ]; then
            HIT_FILES=$(git -C "$repo" -c core.quotepath=false grep --cached -lIF \
                        -f "$VALUES_FILE" "${SCOPE[@]}" 2>/dev/null || true)
            if [ -n "$HIT_FILES" ]; then
                # Печатаем ИМЕНА ФАЙЛОВ, а не строки: строка содержала бы сам ключ.
                printf '%s\n' "$HIT_FILES" | sed 's|^|ЖИВОЙ КЛЮЧ В ФАЙЛЕ: |'
                echo "check-secrets: в коммите лежит значение из $SECRETS_DIR_LOCAL — это И-3. Убери значение, оставь путь к файлу ключа"
                fail=1
            fi
        fi
        rm -f "$VALUES_FILE"
    fi

    if [ "$fail" = 1 ]; then
        echo "check-secrets: ПОХОЖЕ НА СЕКРЕТ В GIT — убери значение в \$SECRETS_DIR, в код клади путь/имя переменной (ложное срабатывание гасится маркером «$MARKER» в строке)"
        return 1
    fi
    echo "check-secrets: чисто"
    return 0
}

# ── самотест: таблица случаев, один прогон по ней ───────────────────────────
#
# ПОЧЕМУ ТАБЛИЦА, А НЕ ДЕСЯТЬ БЛОКОВ. Прежде каждый случай был выписан руками
# по 6-9 строк со своим `echo "selftest N/7"`, и счёт сбился на глазах: шесть
# случаев звали себя «из семи», следом шли «8/8», «9/9», «10/10», а «7/7»
# печатался последним. Хуже счёта был мёртвый флаг: два исхода случая с отсевом
# версий ставили `ok=0`, переменной `ok` в файле нет вовсе — ни объявления, ни
# чтения, — то есть этот случай не мог покраснеть НИКОГДА (ревизия 01.10.2026).
#
# Теперь случай — это ФУНКЦИЯ: она сама готовит стенд, сама судит и возвращает
# 0 или 1. Число случаев печатается из длины списка, а не пишется рукой, и
# провал любого делает самотест красным.
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    trap 'rm -rf "$T"' EXIT
    git -C "$T" init -q

    # Случай 1. Токен в кавычках — красный.
    sluchai_token_v_kavychkah() {
        echo 'token="test123"' > "$T/config.py"  # не-секрет
        git -C "$T" add config.py
        local kod=0; run_scan "$T" all >/dev/null || kod=$?
        rm -f "$T/config.py"; git -C "$T" rm -q --cached config.py
        [ "$kod" != 0 ] || { echo "  токен test123 не пойман"; return 1; }
    }

    # Случай 2. Секрет БЕЗ кавычек (.env/ini-стиль) — раньше проскакивал.
    sluchai_bez_kavychek() {
        echo 'password = supersecret123' > "$T/app.ini"  # не-секрет
        git -C "$T" add app.ini
        local kod=0; run_scan "$T" all >/dev/null || kod=$?
        rm -f "$T/app.ini"; git -C "$T" rm -q --cached app.ini
        [ "$kod" != 0 ] || { echo "  безкавычечный секрет не пойман"; return 1; }
    }

    # Случай 3. Чистый репозиторий обязан пройти: правило, красящее всё подряд,
    # выглядело бы рабочим.
    sluchai_chistyj_repo() {
        echo 'db_password_file = "/etc/x/secrets/db_password"' > "$T/clean.py"
        git -C "$T" add clean.py
        local kod=0; run_scan "$T" all >/dev/null || kod=$?
        rm -f "$T/clean.py"; git -C "$T" rm -q --cached clean.py
        [ "$kod" = 0 ] || { echo "  чистый репозиторий покраснел"; return 1; }
    }

    # Случай 4. Режим --staged: секрет лежит В ИНДЕКСЕ, рабочее дерево чистое.
    # Коммитится индекс — краснеть обязан именно он.
    sluchai_staged() {
        echo 'api_key="AB12cd34ef56"' > "$T/staged.py"  # не-секрет
        git -C "$T" add staged.py
        echo '# чисто' > "$T/staged.py"
        local kod=0; run_scan "$T" staged >/dev/null || kod=$?
        rm -f "$T/staged.py"; git -C "$T" rm -q --cached staged.py
        [ "$kod" != 0 ] || { echo "  --staged не увидел секрет в индексе"; return 1; }
    }

    # Случай 5. Живой пропуск панели 13.09.2026 лежал под кириллическим именем
    # «ПРОПУСК»: имя мимо латинских шаблонов, ловит только ФОРМА значения —
    # 43 знака, как выдаёт secrets.token_urlsafe(32).
    sluchai_forma_bez_imeni() {
        printf 'ПРОПУСК = "%s"\n' "9tzRodPCTLwgGY8Kv2_lDUsIPjDKW0_JMxb_LgI6XXX" > "$T/proba.py"
        git -C "$T" add proba.py
        local kod=0; run_scan "$T" all >/dev/null || kod=$?
        rm -f "$T/proba.py"; git -C "$T" rm -q --cached proba.py
        [ "$kod" != 0 ] || { echo "  секрет без говорящего имени не пойман по форме"; return 1; }
    }

    # Случай 6. «git сломался» отдавалось жёлтым — коммит шёл, а И-3 не
    # проверялся вовсе (ревью пайплайна M-2). Живой .git значит КРАСНЫЙ.
    sluchai_bityj_repozitorij() {
        local bityj; bityj=$(mktemp -d); mkdir -p "$bityj/.git"
        local kod=0; run_scan "$bityj" all >/dev/null 2>&1 || kod=$?
        rm -rf "$bityj"
        [ "$kod" = 1 ] || { echo "  битый репозиторий дал код $kod, а не красный 1"; return 1; }
    }

    # Случай 7. Свежая машина без git — жёлтый 77: ворота не краснеют там, где
    # проверять нечем.
    sluchai_bez_git() {
        local pusto; pusto=$(mktemp -d)
        local kod=0; run_scan "$pusto" all >/dev/null 2>&1 || kod=$?
        rm -rf "$pusto"
        [ "$kod" = 77 ] || { echo "  машина без git дала код $kod, а не жёлтый 77"; return 1; }
    }

    # Случай 8. Имя пакета со словом token и версия следом (pnpm-lock) — НЕ
    # секрет, а настоящий токен в том же файле обязан быть пойман. Погасить
    # строку маркером нельзя: pnpm стирает комментарии при перегенерации.
    sluchai_otsev_versij() {
        printf "      js-tokens: 9.0.1\n" > "$T/pnpm-lock.yaml"  # не-секрет
        printf "  token: a9f3k2x7\n" >> "$T/pnpm-lock.yaml"  # не-секрет: стенд самотеста
        git -C "$T" add pnpm-lock.yaml >/dev/null 2>&1
        local vyvod; vyvod=$(run_scan "$T" all 2>&1 || true)
        rm -f "$T/pnpm-lock.yaml"; git -C "$T" rm -q --cached pnpm-lock.yaml
        if printf %s "$vyvod" | grep -q "js-tokens"; then
            echo "  версия пакета принята за секрет"; return 1
        fi
        printf %s "$vyvod" | grep -q "a9f3k2x7" \
            || { echo "  отсев версий проглотил и НАСТОЯЩИЙ секрет"; return 1; }
    }

    # Случай 9. Значение из каталога ключей попало в файл репозитория. Форма —
    # сорок шестнадцатеричных знаков, неотличимая от git-хэша: шаблоном не
    # поймать, сравнением со значением — можно. Живой случай 29.09.2026: демон
    # карты скопировал сообщение владельца с ключом в dev-map.yaml.
    sluchai_zhivoj_kljuch() {
        local kljuchi repo kod=0
        kljuchi=$(mktemp -d); repo=$(mktemp -d)
        printf '%s\n' 'deadbeefcafebabe0123456789abcdef01234567' > "$kljuchi/kljuch"
        git -C "$repo" init -q
        printf 'summary: "владелец прислал deadbeefcafebabe0123456789abcdef01234567"\n' > "$repo/map.yaml"
        git -C "$repo" add -A
        run_scan "$repo" all "$kljuchi" >/dev/null 2>&1 || kod=$?
        if [ "$kod" = 0 ]; then
            rm -rf "$kljuchi" "$repo"
            echo "  живой ключ в репозитории НЕ пойман — правило мертво"; return 1
        fi
        # Здоровая половина того же правила: тот же репозиторий без значения
        # обязан быть зелёным, иначе правило красит всё подряд.
        printf 'summary: "владелец прислал ключ — он лежит в каталоге ключей"\n' > "$repo/map.yaml"
        git -C "$repo" add -A
        kod=0; run_scan "$repo" all "$kljuchi" >/dev/null 2>&1 || kod=$?
        rm -rf "$kljuchi" "$repo"
        [ "$kod" = 0 ] || { echo "  ложное срабатывание на тексте без значения"; return 1; }
    }

    # Случай 10. ФОРМАТЫ ЧУЖИХ СИСТЕМ. Их в правилах шесть — телеграм, AWS,
    # Google, GitHub (два вида), Slack и PEM-ключ, — и до 02.10.2026 ни один из
    # них не судил ни один случай: поломка, стёршая шаблон токена телеграма,
    # оставляла самотест зелёным. Формат проверяется ПООЧЕРЁДНО и называется
    # по имени: один зелёный формат не должен прятать пять слепых.
    sluchai_formaty_chuzhih_sistem() {
        # Образцы ВЫДУМАНЫ по форме каждого формата и помечены маркером: это
        # стенд самотеста, а не ключи. Маркер здесь обязателен — иначе гейт
        # краснеет на собственном файле (поймано им же 02.10.2026).
        local obrazcy=(
            "телеграм:1234567890:AAH9zQmKX1vJdQn7p-RtYuIoP3sDfGhJkL0"  # не-секрет
            "aws:AKIAIOSFODNN7EXAMPLE"  # не-секрет
            "google:AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"  # не-секрет
            "github-pat:ghp_ABcdEFghIJklMNopQRstUVwxYZ0123456789"  # не-секрет
            "github-fine:github_pat_11ABCDEFG0abcdefghij_KLMNOPQR"  # не-секрет
            "slack:xoxb-1234567890-abcdefGHIJKL"  # не-секрет
            "pem:-----BEGIN RSA PRIVATE KEY-----"  # не-секрет
        )
        local zapis imya znachenie kod slepye=""
        for zapis in "${obrazcy[@]}"; do
            imya="${zapis%%:*}"
            znachenie="${zapis#*:}"
            printf '%s\n' "$znachenie" > "$T/chuzhoj.txt"
            git -C "$T" add chuzhoj.txt
            kod=0; run_scan "$T" all >/dev/null 2>&1 || kod=$?
            rm -f "$T/chuzhoj.txt"; git -C "$T" rm -q --cached chuzhoj.txt
            [ "$kod" != 0 ] || slepye="$slepye $imya"
        done
        [ -z "$slepye" ] || { echo "  форматы прошли мимо правил:$slepye"; return 1; }
    }

    # ТАБЛИЦА СЛУЧАЕВ. Число печатается из её длины: рукописное «N/7» уже
    # разъезжалось с действительностью, и заметить это было некому.
    SLUCHAI=(
        "токен в кавычках — красный:sluchai_token_v_kavychkah"
        "секрет без кавычек — красный:sluchai_bez_kavychek"
        "чистый репозиторий — зелёный:sluchai_chistyj_repo"
        "секрет из индекса при чистом дереве — красный:sluchai_staged"
        "секрет без говорящего имени, по форме — красный:sluchai_forma_bez_imeni"
        "битый репозиторий — красный, не жёлтый:sluchai_bityj_repozitorij"
        "машина без git — жёлтый 77:sluchai_bez_git"
        "версия пакета не секрет, токен рядом — красный:sluchai_otsev_versij"
        "значение из каталога ключей в репозитории — красный:sluchai_zhivoj_kljuch"
        "форматы чужих систем ловятся все семь:sluchai_formaty_chuzhih_sistem"
    )

    vsego=${#SLUCHAI[@]}
    neudach=0
    nomer=0
    for zapis in "${SLUCHAI[@]}"; do
        nomer=$((nomer + 1))
        imya="${zapis%%:*}"
        funkciya="${zapis##*:}"
        if "$funkciya"; then
            echo "selftest $nomer/$vsego: $imya — OK"
        else
            echo "selftest $nomer/$vsego: $imya — ПЛОХО"
            neudach=$((neudach + 1))
        fi
    done

    if [ "$neudach" != 0 ]; then
        echo "SELFTEST FAIL: случаев $vsego, неудач $neudach"
        exit 1
    fi
    echo "SELFTEST: зелёный ($vsego случаев, неудач 0)"
    exit 0
fi

# ── боевой запуск ───────────────────────────────────────────────────────────
MODE=all
REPO=""
if [ "${1:-}" = "--staged" ]; then
    MODE=staged; shift
fi
REPO="${1:-}"
if [ -z "$REPO" ]; then
    # Каталог сканирования — СТРОГО из паспорта установки, мимо окружения:
    # сторожу инварианта И-3 окружение не хозяин. Переменная с родовым именем
    # PROJECT_DIR увела бы проверку секретов на чужое дерево, и она молча
    # отчиталась бы «чисто» (ревью кода 12.09.2026, F1-gen-03).
    # shellcheck disable=SC1091
    source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
    REPO="$(konf_iz_fajla PROJECT_DIR)"
    REPO="${REPO:-$PWD}"
    # Подмена обязана быть ВИДНА, а не молчать.
    [ -n "${PROJECT_DIR:-}" ] && [ "${PROJECT_DIR}" != "$REPO" ] && \
        echo "check-secrets: сканирую $REPO (из паспорта); PROJECT_DIR в окружении — $PROJECT_DIR, он не в счёт"
fi
run_scan "$REPO" "$MODE"
