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
# Код возврата: 0 — чисто, 1 — найден похожий на секрет текст (или сбой git:
# «не проверено» красное, не жёлтое), 77 — не git-репозиторий.
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

# Лимит — не секрет: значение — счёт токенов модели или размер, а не ключ.
# Отсев узкий и привязан к ИМЕНИ в начале присваивания (хвост строки его не гасит): имя
# кончается на «tokens» (мн. число — счёт) или на слово-меру, значение целиком из цифр.
# Одиночное имя TOKEN с числом и любой буквенно-цифровой токен по-прежнему красные.
# Сравнение без учёта регистра (имена в .env заглавные), поэтому -i у вызова.
CHISLO='^[^:]*:[0-9]+:[[:space:]]*(export[[:space:]]+)?["'"'"']?[A-Za-z0-9_.-]*(tokens|(token|secret|passwd|password|api_?key)[A-Za-z0-9_-]*_(limit|max|min|size|count|ttl|len|timeout))["'"'"']?[[:space:]]*[:=][[:space:]]*"?[0-9]+"?[[:space:]]*$'

# Находка печатается как «СЕКРЕТ? путь:строка: вид [скрыто]» — значение в вывод
# не идёт: вывод гейта оседает в журналах ворот, в выводе publish.sh и в
# транскрипте сессии, а напечатанный секрет и есть утечка (И-3). Вид узнаётся
# по тому же шаблону, что поймал строку; специфичные форматы проверяются
# раньше общего присвоения, иначе «token = ghp_…» назвался бы присвоением.
KINDS=(
    'telegram|[0-9]{8,10}:AA[0-9A-Za-z_-]{33}'
    'aws|AKIA[0-9A-Z]{16}'
    'google|AIza[0-9A-Za-z_-]{35}'
    'github|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,}'
    'slack|xox[baprs]-[0-9A-Za-z-]{10,}'
    'pem|-----BEGIN [A-Z ]*PRIVATE KEY-----'
    'форма-43|=[[:space:]]*["'"'"'][A-Za-z0-9_-]{43}["'"'"']'
)
mask_findings() {  # stdin: строки git grep «путь:номер:текст»
    local row location text kind entry name pattern
    while IFS= read -r row; do
        location="${row%%:*}"; text="${row#*:}"      # путь
        location="$location:${text%%:*}"; text="${text#*:}"  # путь:номер
        kind="присвоение"
        for entry in "${KINDS[@]}"; do
            name="${entry%%|*}"; pattern="${entry#*|}"
            if [[ "$text" =~ $pattern ]]; then kind="$name"; break; fi
        done
        printf 'СЕКРЕТ? %s: %s [скрыто]\n' "$location" "$kind"
    done
}

# Конфиг источников → строки «вид<TAB>путь<TAB>обязателен». Запись начинается
# строкой «- вид:», поля идут под ней; комментарии — целыми строками.
parse_sources() {  # $1 = файл конфига
    awk '
        function flush() { if (kind != "") print kind "\t" path "\t" (req == "" ? "нет" : req); kind = path = req = "" }
        function val(l) { sub(/^[^:]*:[ \t]*/, "", l); sub(/[ \t\r]+$/, "", l); return l }
        /^[ \t]*#/ { next }
        /^- вид:/ { flush(); kind = val($0); next }
        /^[ \t]+путь:/ { path = val($0); next }
        /^[ \t]+обязателен:/ { req = val($0); next }
        END { flush() }
    ' "$1"
}

# Поиск значений из файла $2 по индексу одним `git grep -F -f`: вывод
# «путь\0номер\0строка\n» уходит в файл $3. Код git больше 1 — это поломка, и
# «чисто» тут было бы враньём (И-3 держится этим гейтом): возврат 4 = красный.
# --literal-pathspecs: имя файла со скобками или «:(» — не шаблон пути.
git_grep_values() {  # $1 репо, $2 файл значений, $3 файл вывода, $4.. область git grep
    local repo="$1" valfile="$2" out="$3" rc=0; shift 3
    git --literal-pathspecs -C "$repo" -c core.quotepath=false grep --cached -nzIF -f "$valfile" "$@" \
        > "$out" 2>/dev/null || rc=$?
    if [ "$rc" -gt 1 ]; then
        echo "check-secrets: git grep упал (код $rc) — значения из источника НЕ проверены"
        return 4
    fi
}

# Источник «каталог»: файлы целиком от 16 знаков, вплоть до 3 уровней вглубь,
# симлинки по ссылке; из JSON и «ключ: значение» берутся и поля строки, а
# строка без пробелов — ещё и целиком (токен Telegram «123:AA…» с двоеточием).
# Возврат: 0 — чисто, 1 — найдено, 3 — каталог есть, а проверить из него нечего
# или не всё (нечитаемый файл, нет годных значений) — предупреждение; 4 — сбой git.
# Каталога нет совсем — тихо 0: необязательный источник на этой машине не заведён.
scan_dir_source() {  # $1 репо, $2 каталог, $3.. область git grep
    local repo="$1" dir="$2" f path lno text unread=0 names
    local raw="$TMPD/dir.raw" values="$TMPD/dir.values" hits="$TMPD/dir.hits"; shift 2
    [ -d "$dir" ] || return 0
    # Каждый файл читается ОТДЕЛЬНО: файл ключа без перевода строки при общем
    # `cat` склеился бы со следующим, и ни один из двух не нашёлся бы.
    # Значения читаются, но НЕ печатаются: напечатанный ключ и есть утечка.
    : > "$raw"
    while IFS= read -r -d '' f; do
        [ -f "$f" ] || continue
        if [ -r "$f" ]; then awk '1' "$f" >> "$raw"; else unread=$((unread + 1)); fi
    done < <(find "$dir" -maxdepth 3 \( -type f -o -type l \) -print0 2>/dev/null)
    tr -d '\r' < "$raw" | awk '
        function emit(s) { if (length(s) >= 16) print s }
        # Адрес почты и ссылка в JSON ключа (client_email, token_uri) — не секрет,
        # а в документах они встречаются законно: поле их вида не берётся.
        function field(s) { if (s !~ /@|^[A-Za-z][A-Za-z0-9+.-]*:\/\//) emit(s) }
        { if ($0 !~ /[ \t]/) emit($0)
          n = split($0, a, /[ \t,"\047{}]+/)
          for (i = 1; i <= n; i++) { field(a[i]); p = index(a[i], "="); if (p) field(substr(a[i], p + 1)) } }
    ' | sort -u > "$values"
    [ "$unread" = 0 ] || echo "check-secrets: ПРЕДУПРЕЖДЕНИЕ: в $dir нечитаемых файлов ключей: $unread — значения из них не проверены"
    if [ ! -s "$values" ]; then
        echo "check-secrets: ПРЕДУПРЕЖДЕНИЕ: в каталоге $dir нет значений от 16 знаков — проверять нечего"
        return 3
    fi
    git_grep_values "$repo" "$values" "$hits" "$@" || return 4
    # Печатаем ИМЕНА ФАЙЛОВ, а не строки: строка содержала бы сам ключ.
    names=$(while IFS= read -r -d '' path && IFS= read -r -d '' lno && IFS= read -r text; do
                printf '%s\n' "$path"
            done < "$hits" | sort -u)
    if [ -n "$names" ]; then
        printf '%s\n' "$names" | sed 's|^|ЖИВОЙ КЛЮЧ В ФАЙЛЕ: |'
        echo "check-secrets: в коммите лежит значение из $dir — это И-3. Убери значение, оставь путь к файлу ключа"
        return 1
    fi
    [ "$unread" = 0 ] || return 3
}

# Источник «env»: значения ключей с pass/pw/token/secret/key/auth/cred в имени,
# от 6 знаков. Значение — после снятия кавычек и комментария в конце строки.
# Поиск ОДНИМ вызовом git; ключ находке называется по совпавшей строке.
# Возврат: 0 — чисто, 1 — найдено, 3 — подходящих значений нет, 4 — сбой git.
scan_env_source() {  # $1 репо, $2 файл, $3.. область git grep
    local repo="$1" envfile="$2" key value path lno text self rel found=0
    local pairs="$TMPD/env.pairs" valfile="$TMPD/env.values" hits="$TMPD/env.hits"; shift 2
    awk '
        function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
        {
            sub(/\r$/, ""); l = $0
            sub(/^[ \t]*(export[ \t]+)?/, "", l)
            if (l ~ /^#/) next
            i = index(l, "="); if (!i) next
            k = trim(substr(l, 1, i - 1)); r = substr(l, i + 1); sub(/^[ \t]+/, "", r)
            if (tolower(k) !~ /pass|pw|token|secret|key|auth|cred/) next
            q = substr(r, 1, 1)
            if (q == "\"") {
                v = ""; n = length(r)
                for (j = 2; j <= n; j++) {
                    c = substr(r, j, 1)
                    if (c == "\\" && j < n) { j++; v = v substr(r, j, 1); continue }
                    if (c == "\"") break
                    v = v c
                }
            } else if (q == "\047") {
                v = substr(r, 2); p = index(v, "\047"); if (p) v = substr(v, 1, p - 1)
            } else { v = r; sub(/[ \t]+#.*$/, "", v); v = trim(v) }
            if (length(v) < 6) next
            print k "\t" v
        }' "$envfile" > "$pairs" 2>/dev/null || true
    if [ ! -s "$pairs" ]; then
        echo "check-secrets: ПРЕДУПРЕЖДЕНИЕ: в источнике $envfile нет ключей с PASSWORD/TOKEN/SECRET/KEY и значением от 6 знаков — проверять нечего"
        return 3
    fi
    cut -f2- "$pairs" | sort -u > "$valfile"
    git_grep_values "$repo" "$valfile" "$hits" "$@" || return 4
    # Сам источник, если он вдруг лежит в репо, не считается находкой.
    self=$(realpath -m "$envfile"); rel=""
    case "$self" in "$(realpath -m "$repo")"/*) rel="${self#"$(realpath -m "$repo")"/}" ;; esac
    while IFS= read -r -d '' path && IFS= read -r -d '' lno && IFS= read -r text; do
        [ "$path" != "$rel" ] || continue
        while IFS=$'\t' read -r key value; do
            [[ "$text" == *"$value"* ]] || continue
            printf 'ЗНАЧЕНИЕ ИЗ ИСТОЧНИКА: %s:%s: значение из %s, ключ %s\n' "$path" "$lno" "$envfile" "$key"
            found=1
        done < "$pairs"
    done < "$hits"
    [ "$found" = 0 ]
}

run_scan_inner() {  # $1 = каталог репо, $2 = режим all|staged, $3 = каталог ключей (по умолчанию — из паспорта),
                #  $4 = конфиг источников значений (по умолчанию secret-sources.yaml рядом)
    local repo="$1" mode="$2" fail=0 hits f rc
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
        # Всё, кроме удалённых (d): изменённый M, переименованный R и сменивший
        # тип T несут новое содержимое не хуже добавленного. Имена — через NUL:
        # кавычка, обратная черта и табуляция в имени иначе приходят экранированными.
        git -C "$repo" -c core.quotepath=false diff --cached --name-only -z --diff-filter=d \
            > "$TMPD/names" || { echo "check-secrets: git diff упал — секреты НЕ проверены"; return 1; }
        [ -s "$TMPD/names" ] || { echo "check-secrets: файлов нет — чисто"; return 0; }
        # Ограничиваем область именно изменёнными файлами: остальное уже в репо
        # и этим коммитом не вносится.
        SCOPE=(--)
        while IFS= read -r -d '' f; do SCOPE+=("$f"); done < "$TMPD/names"
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
    # Под set -x присвоение hits и разбор в mask_findings показали бы в трассировке
    # строку с самим значением.
    local xtrace_hits=0
    case $- in *x*) xtrace_hits=1; set +x ;; esac
    hits=""; rc=0
    git --literal-pathspecs -C "$repo" -c core.quotepath=false grep --cached -inI "$RE_FLAG" \
        "${PATTERNS[@]}" "${SCOPE[@]}" > "$TMPD/patterns.out" 2>/dev/null || rc=$?
    if [ "$rc" -gt 1 ]; then
        echo "check-secrets: git grep упал (код $rc) — секреты НЕ проверены"
        fail=1
    else
        hits=$(grep -v "$MARKER" "$TMPD/patterns.out" | grep -vE "$VERSIYA" | grep -viE "$CHISLO" || true)
    fi
    if [ -n "$hits" ]; then
        mask_findings <<< "$hits"
        fail=1
    fi
    [ "$xtrace_hits" = 0 ] || set -x
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
    # Источники значений — данные (secret-sources.yaml), не код: новый носитель
    # секрета добавляется строкой в конфиг. Само значение живёт только в
    # временных файлах 600 и в аргументах builtin printf; в вывод оно не идёт.
    local sources_conf="${4:-}" warned=0 kind src_path required
    if [ -z "$sources_conf" ]; then
        local here; here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
        # В проекте конфиги лежат в harness/config рядом со scripts/, в пакете
        # скрипт сам лежит в harness/scripts/, а конфиги — в harness/config.
        for sources_conf in "$here/../harness/config/secret-sources.yaml" "$here/../config/secret-sources.yaml"; do
            [ -r "$sources_conf" ] && break
        done
    fi
    if [ ! -r "$sources_conf" ]; then
        echo "check-secrets: ПРЕДУПРЕЖДЕНИЕ: конфиг источников значений не найден — точные значения секретов не проверены"
        warned=1
    else
        local xtrace_was_on=0
        case $- in *x*) xtrace_was_on=1; set +x ;; esac  # значение не должно попасть в трассировку
        while IFS=$'\t' read -r kind src_path required; do
            [ -n "$kind" ] || continue
            if [ "$kind" = "каталог" ] && [ "$src_path" = "SECRETS_DIR" ]; then
                src_path="${3:-}"
                if [ -z "$src_path" ]; then
                    # shellcheck disable=SC1091
                    source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
                    src_path="$(konf_iz_fajla SECRETS_DIR)"
                fi
                [ -n "$src_path" ] || continue
            fi
            if [ ! -e "$src_path" ]; then
                if [ "$required" = "да" ]; then
                    echo "check-secrets: ПРЕДУПРЕЖДЕНИЕ: обязательный источник $src_path отсутствует — значения из него не проверены"
                    warned=1
                fi
                continue
            fi
            if [ ! -r "$src_path" ]; then
                echo "check-secrets: ПРЕДУПРЕЖДЕНИЕ: источник $src_path нечитаем — значения из него не проверены"
                warned=1
                continue
            fi
            case "$kind" in
                env)     scan_env_source "$repo" "$src_path" "${SCOPE[@]}" || { rc=$?; [ "$rc" = 3 ] && warned=1 || fail=1; } ;;
                каталог) scan_dir_source "$repo" "$src_path" "${SCOPE[@]}" || { rc=$?; [ "$rc" = 3 ] && warned=1 || fail=1; } ;;
                *) echo "check-secrets: ПРЕДУПРЕЖДЕНИЕ: источник $src_path: неизвестный вид «$kind» — не проверен"; warned=1 ;;
            esac
        done < <(parse_sources "$sources_conf")
        [ "$xtrace_was_on" = 1 ] && set -x
    fi

    if [ "$fail" = 1 ]; then
        echo "check-secrets: ПОХОЖЕ НА СЕКРЕТ В GIT — убери значение в \$SECRETS_DIR, в код клади путь/имя переменной (ложное срабатывание гасится маркером «$MARKER» в строке)"
        return 1
    fi
    if [ "$warned" = 1 ]; then
        echo "check-secrets: чисто, НО источники проверены не все (см. ПРЕДУПРЕЖДЕНИЕ выше)"
    else
        echo "check-secrets: чисто"
    fi
    return 0
}

# Прогон в подоболочке: временные файлы со значениями живут в одном каталоге,
# который убирается при любом выходе, включая SIGTERM и Ctrl-C (bash гонит
# ловушку EXIT и по смертельному сигналу; `trap RETURN` сигнал не ловил, и
# значения оставались в /tmp). Подоболочка заодно делает уборку независимой от
# ловушек вызывающего.
run_scan() {
    (
        TMPD=$(mktemp -d -t check-secrets-scan-XXXXXX)
        trap 'rm -rf "$TMPD"' EXIT
        run_scan_inner "$@"
    )
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
    T=$(mktemp -d -t proba-check-secrets-XXXXXX)
    C=$(mktemp -d -t proba-check-secrets-XXXXXX)
    trap 'rm -rf "$T" "$C"' EXIT
    git -C "$T" init -q
    # Стендовые конфиги источников: без них run_scan читал бы боевой
    # /srv/sample-18/.env, и исход самотеста зависел бы от машины.
    KONF_PUSTOJ="$C/pustoj.yaml"; printf '# источников нет\n' > "$KONF_PUSTOJ"
    KONF_KATALOG="$C/katalog.yaml"; printf -- '- вид: каталог\n  путь: SECRETS_DIR\n' > "$KONF_KATALOG"

    # Случай 1. Токен в кавычках — красный.
    sluchai_token_v_kavychkah() {
        echo 'token="test123"' > "$T/config.py"  # не-секрет
        git -C "$T" add config.py
        local kod=0; run_scan "$T" all "" "$KONF_PUSTOJ" >/dev/null || kod=$?
        rm -f "$T/config.py"; git -C "$T" rm -q --cached config.py
        [ "$kod" != 0 ] || { echo "  токен test123 не пойман"; return 1; }
    }

    # Случай 2. Секрет БЕЗ кавычек (.env/ini-стиль) — раньше проскакивал.
    sluchai_bez_kavychek() {
        echo 'password = supersecret123' > "$T/app.ini"  # не-секрет
        git -C "$T" add app.ini
        local kod=0; run_scan "$T" all "" "$KONF_PUSTOJ" >/dev/null || kod=$?
        rm -f "$T/app.ini"; git -C "$T" rm -q --cached app.ini
        [ "$kod" != 0 ] || { echo "  безкавычечный секрет не пойман"; return 1; }
    }

    # Случай 3. Чистый репозиторий обязан пройти: правило, красящее всё подряд,
    # выглядело бы рабочим.
    sluchai_chistyj_repo() {
        echo 'db_password_file = "/etc/x/secrets/db_password"' > "$T/clean.py"
        git -C "$T" add clean.py
        local kod=0; run_scan "$T" all "" "$KONF_PUSTOJ" >/dev/null || kod=$?
        rm -f "$T/clean.py"; git -C "$T" rm -q --cached clean.py
        [ "$kod" = 0 ] || { echo "  чистый репозиторий покраснел"; return 1; }
    }

    # Случай 4. Режим --staged: секрет лежит В ИНДЕКСЕ, рабочее дерево чистое.
    # Коммитится индекс — краснеть обязан именно он.
    sluchai_staged() {
        echo 'api_key="AB12cd34ef56"' > "$T/staged.py"  # не-секрет
        git -C "$T" add staged.py
        echo '# чисто' > "$T/staged.py"
        local kod=0; run_scan "$T" staged "" "$KONF_PUSTOJ" >/dev/null || kod=$?
        rm -f "$T/staged.py"; git -C "$T" rm -q --cached staged.py
        [ "$kod" != 0 ] || { echo "  --staged не увидел секрет в индексе"; return 1; }
    }

    # Случай 5. Живой пропуск панели 13.09.2026 лежал под кириллическим именем
    # «ПРОПУСК»: имя мимо латинских шаблонов, ловит только ФОРМА значения —
    # 43 знака, как выдаёт secrets.token_urlsafe(32).
    sluchai_forma_bez_imeni() {
        printf 'ПРОПУСК = "%s"\n' "9tzRodPCTLwgGY8Kv2_lDUsIPjDKW0_JMxb_LgI6XXX" > "$T/proba.py"
        git -C "$T" add proba.py
        local kod=0; run_scan "$T" all "" "$KONF_PUSTOJ" >/dev/null || kod=$?
        rm -f "$T/proba.py"; git -C "$T" rm -q --cached proba.py
        [ "$kod" != 0 ] || { echo "  секрет без говорящего имени не пойман по форме"; return 1; }
    }

    # Случай 6. «git сломался» отдавалось жёлтым — коммит шёл, а И-3 не
    # проверялся вовсе (ревью пайплайна M-2). Живой .git значит КРАСНЫЙ.
    sluchai_bityj_repozitorij() {
        local bityj; bityj=$(mktemp -d); mkdir -p "$bityj/.git"
        local kod=0; run_scan "$bityj" all "" "$KONF_PUSTOJ" >/dev/null 2>&1 || kod=$?
        rm -rf "$bityj"
        [ "$kod" = 1 ] || { echo "  битый репозиторий дал код $kod, а не красный 1"; return 1; }
    }

    # Случай 7. Свежая машина без git — жёлтый 77: ворота не краснеют там, где
    # проверять нечем.
    sluchai_bez_git() {
        local pusto; pusto=$(mktemp -d)
        local kod=0; run_scan "$pusto" all "" "$KONF_PUSTOJ" >/dev/null 2>&1 || kod=$?
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
        local vyvod; vyvod=$(run_scan "$T" all "" "$KONF_PUSTOJ" 2>&1 || true)
        rm -f "$T/pnpm-lock.yaml"; git -C "$T" rm -q --cached pnpm-lock.yaml
        # Значение в выводе не печатается, поэтому судим по номеру строки:
        # версия — строка 1, настоящий токен — строка 2.
        if printf %s "$vyvod" | grep -q 'pnpm-lock.yaml:1:'; then
            echo "  версия пакета принята за секрет"; return 1
        fi
        printf %s "$vyvod" | grep -q 'pnpm-lock.yaml:2:' \
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
        run_scan "$repo" all "$kljuchi" "$KONF_KATALOG" >/dev/null 2>&1 || kod=$?
        if [ "$kod" = 0 ]; then
            rm -rf "$kljuchi" "$repo"
            echo "  живой ключ в репозитории НЕ пойман — правило мертво"; return 1
        fi
        # Здоровая половина того же правила: тот же репозиторий без значения
        # обязан быть зелёным, иначе правило красит всё подряд.
        printf 'summary: "владелец прислал ключ — он лежит в каталоге ключей"\n' > "$repo/map.yaml"
        git -C "$repo" add -A
        kod=0; run_scan "$repo" all "$kljuchi" "$KONF_KATALOG" >/dev/null 2>&1 || kod=$?
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
            kod=0; run_scan "$T" all "" "$KONF_PUSTOJ" >/dev/null 2>&1 || kod=$?
            rm -f "$T/chuzhoj.txt"; git -C "$T" rm -q --cached chuzhoj.txt
            [ "$kod" != 0 ] || slepye="$slepye $imya"
        done
        [ -z "$slepye" ] || { echo "  форматы прошли мимо правил:$slepye"; return 1; }
    }

    # ── пути «значения из источников» ───────────────────────────────────────
    # Каждый путь строит СВОЙ стенд: репо, конфиг источников и .env лежат в
    # временном каталоге, значение секрета ГЕНЕРИРУЕТСЯ на месте — боевой
    # секрет в стенд не списывается и от машины ничего не зависит.
    stend_novyj() { S=$(mktemp -d); mkdir "$S/repo"; git -C "$S/repo" init -q; }
    stend_znachenie() {
        if command -v openssl >/dev/null 2>&1; then printf 'v%s' "$(openssl rand -hex 10)"
        else printf 'v%s%s%s%s' "$RANDOM" "$RANDOM" "$RANDOM" "$RANDOM"; fi
    }
    stend_konf() {  # $1 файл конфига, $2 путь источника env, $3 обязателен
        printf -- '- вид: env\n  путь: %s\n  обязателен: %s\n' "$2" "${3:-нет}" > "$1"
    }

    # Случай 11. Больной: значение из .env лежит в staged-файле. Красный,
    # названы файл, строка, источник и ключ, а самого значения в выводе нет.
    sluchai_znachenie_iz_istochnika() {
        local S znach kod vyvod ok=0
        stend_novyj; znach=$(stend_znachenie)
        printf 'GAME_PASSWORD=%s\n' "$znach" > "$S/.env"
        stend_konf "$S/src.yaml" "$S/.env"
        printf 'строка один\nсервер: %s\n' "$znach" > "$S/repo/notes.txt"
        git -C "$S/repo" add notes.txt
        for mode in all staged; do
            kod=0; vyvod=$(run_scan "$S/repo" "$mode" "" "$S/src.yaml" 2>&1) || kod=$?
            [ "$kod" = 1 ] || { echo "  режим $mode: код $kod, а не красный 1"; ok=1; }
            printf %s "$vyvod" | grep -qF 'notes.txt:2:' || { echo "  режим $mode: не назван файл:строка"; ok=1; }
            printf %s "$vyvod" | grep -qF "$S/.env" || { echo "  режим $mode: не назван источник"; ok=1; }
            printf %s "$vyvod" | grep -qF 'GAME_PASSWORD' || { echo "  режим $mode: не назван ключ"; ok=1; }
            ! printf %s "$vyvod" | grep -qF "$znach" || { echo "  режим $mode: ЗНАЧЕНИЕ НАПЕЧАТАНО"; ok=1; }
        done
        rm -rf "$S"; return "$ok"
    }

    # Случай 12. Здоровый: значения нет в файлах — зелёный и без оговорок.
    sluchai_istochnik_zdorovyj() {
        local S kod=0 vyvod
        stend_novyj
        printf 'GAME_PASSWORD=%s\n' "$(stend_znachenie)" > "$S/.env"
        stend_konf "$S/src.yaml" "$S/.env"
        printf 'ключ лежит в .env, здесь только путь\n' > "$S/repo/notes.txt"
        git -C "$S/repo" add notes.txt
        vyvod=$(run_scan "$S/repo" all "" "$S/src.yaml" 2>&1) || kod=$?
        rm -rf "$S"
        [ "$kod" = 0 ] || { echo "  здоровый стенд дал код $kod"; return 1; }
        [ "$vyvod" = "check-secrets: чисто" ] || { echo "  здоровый стенд с оговоркой: $vyvod"; return 1; }
    }

    # Случай 13. Источник задан, но нечитаем: каталог вместо файла и файл с
    # правами 000. Зелёный без слова — это слепота, нужно предупреждение.
    sluchai_istochnik_nechitaem() {
        local S kod vyvod ok=0 variant
        stend_novyj
        mkdir "$S/dir.env"
        printf 'GAME_PASSWORD=%s\n' "$(stend_znachenie)" > "$S/locked.env"; chmod 000 "$S/locked.env"
        for variant in "$S/dir.env" "$S/locked.env"; do
            # root читает и файл с правами 000: вариант судим только если файл
            # действительно нечитаем для нас.
            if [ -f "$variant" ] && [ -r "$variant" ]; then echo "  вариант $variant читаем (запуск от root) — не судится"; continue; fi
            stend_konf "$S/src.yaml" "$variant"
            kod=0; vyvod=$(run_scan "$S/repo" all "" "$S/src.yaml" 2>&1) || kod=$?
            [ "$kod" = 0 ] || { echo "  $variant: код $kod"; ok=1; }
            printf %s "$vyvod" | grep -qF 'ПРЕДУПРЕЖДЕНИЕ' || { echo "  $variant: нечитаемый источник прошёл молча"; ok=1; }
            printf %s "$vyvod" | grep -qF 'НО источники проверены не все' || { echo "  $variant: итог не оговорён"; ok=1; }
        done
        chmod 600 "$S/locked.env"; rm -rf "$S"; return "$ok"
    }

    # Случай 14. Источника нет и он необязателен — тихо зелёный; обязательный
    # без файла — предупреждение.
    sluchai_istochnika_net() {
        local S kod=0 vyvod ok=0
        stend_novyj
        stend_konf "$S/src.yaml" "$S/net-takogo.env" "нет"
        vyvod=$(run_scan "$S/repo" all "" "$S/src.yaml" 2>&1) || kod=$?
        { [ "$kod" = 0 ] && [ "$vyvod" = "check-secrets: чисто" ]; } || { echo "  необязательный без файла: код $kod, вывод: $vyvod"; ok=1; }
        stend_konf "$S/src.yaml" "$S/net-takogo.env" "да"
        kod=0; vyvod=$(run_scan "$S/repo" all "" "$S/src.yaml" 2>&1) || kod=$?
        printf %s "$vyvod" | grep -qF 'ПРЕДУПРЕЖДЕНИЕ' || { echo "  обязательный без файла прошёл молча"; ok=1; }
        rm -rf "$S"; return "$ok"
    }

    # Случай 15. Значение короче 6 знаков не ищется. В .env есть и длинное
    # значение (чтобы источник не считался пустым): судим именно отсев короткого.
    sluchai_korotkoe_znachenie() {
        local S kod=0 vyvod
        stend_novyj
        printf 'GAME_PASSWORD=%s\nSHORT_TOKEN=q7x9z\n' "$(stend_znachenie)" > "$S/.env"  # не-секрет
        stend_konf "$S/src.yaml" "$S/.env"
        printf 'при чём тут q7x9z\n' > "$S/repo/notes.txt"
        git -C "$S/repo" add notes.txt
        vyvod=$(run_scan "$S/repo" all "" "$S/src.yaml" 2>&1) || kod=$?
        rm -rf "$S"
        [ "$kod" = 0 ] || { echo "  значение из 5 знаков найдено (код $kod)"; return 1; }
    }

    # Случай 16. Ключ без PASSWORD/TOKEN/SECRET/KEY в имени не берётся: имя
    # сервера и порт — не секреты, их значения в репо законны.
    sluchai_kljuch_bez_slova() {
        local S kod=0 vyvod znach
        stend_novyj; znach=$(stend_znachenie)
        printf 'GAME_PASSWORD=%s\nSERVER_NAME=%s\n' "$(stend_znachenie)" "$znach" > "$S/.env"
        stend_konf "$S/src.yaml" "$S/.env"
        printf 'сервер называется %s\n' "$znach" > "$S/repo/notes.txt"
        git -C "$S/repo" add notes.txt
        vyvod=$(run_scan "$S/repo" all "" "$S/src.yaml" 2>&1) || kod=$?
        rm -rf "$S"
        [ "$kod" = 0 ] || { echo "  значение ключа SERVER_NAME принято за секрет (код $kod)"; return 1; }
    }

    # Случай 17. Конфиг источников не найден: гейт не краснеет (машина без
    # конфига — не поломка коммита), но и не молчит — предупреждение и оговорка.
    sluchai_konfig_ne_najden() {
        local S kod=0 vyvod ok=0
        stend_novyj
        vyvod=$(run_scan "$S/repo" all "" "$S/net-konfiga.yaml" 2>&1) || kod=$?
        rm -rf "$S"
        [ "$kod" = 0 ] || { echo "  код $kod, а не 0"; ok=1; }
        printf %s "$vyvod" | grep -qF 'конфиг источников значений не найден' || { echo "  нет предупреждения о конфиге"; ok=1; }
        printf %s "$vyvod" | grep -qF 'НО источники проверены не все' || { echo "  итог не оговорён"; ok=1; }
        return "$ok"
    }

    # Случай 18. Маркер «не-секрет» гасит ложное срабатывание ШАБЛОНА, но не
    # точное совпадение со значением из источника: строка с маркером и
    # настоящим значением обязана краснеть.
    sluchai_marker_ne_gasit_znachenie() {
        local S znach kod=0 vyvod
        stend_novyj; znach=$(stend_znachenie)
        printf 'GAME_PASSWORD=%s\n' "$znach" > "$S/.env"
        stend_konf "$S/src.yaml" "$S/.env"
        printf 'сервер: %s  # не-секрет\n' "$znach" > "$S/repo/notes.txt"
        git -C "$S/repo" add notes.txt
        vyvod=$(run_scan "$S/repo" all "" "$S/src.yaml" 2>&1) || kod=$?
        rm -rf "$S"
        [ "$kod" = 1 ] || { echo "  маркер погасил точное значение (код $kod)"; return 1; }
    }

    # Случай 19. Источник есть, но подходящих ключей в нём нет: предупреждение
    # о том, что проверять нечего, а не слово «нечитаем».
    sluchai_istochnik_bez_klyuchej() {
        local S kod=0 vyvod ok=0
        stend_novyj
        printf 'SERVER_NAME=%s\n' "$(stend_znachenie)" > "$S/.env"
        stend_konf "$S/src.yaml" "$S/.env"
        vyvod=$(run_scan "$S/repo" all "" "$S/src.yaml" 2>&1) || kod=$?
        rm -rf "$S"
        [ "$kod" = 0 ] || { echo "  код $kod"; ok=1; }
        printf %s "$vyvod" | grep -qF 'проверять нечего' || { echo "  нет сообщения «проверять нечего»"; ok=1; }
        ! printf %s "$vyvod" | grep -qF 'нечитаем' || { echo "  читаемый файл назван нечитаемым"; ok=1; }
        return "$ok"
    }

    # Случай 20. Под `set -x` значение не попадает в трассировку ни по старой
    # ветке (шаблоны, mask_findings), ни по новой (значения из источников).
    sluchai_xtrace_bez_znacheniya() {
        local S znach kod=0 vyvod ok=0
        stend_novyj; znach=$(stend_znachenie)
        printf 'GAME_PASSWORD=%s\n' "$znach" > "$S/.env"
        stend_konf "$S/src.yaml" "$S/.env"
        printf 'token="%s"\n' "$znach" > "$S/repo/config.py"  # не-секрет
        git -C "$S/repo" add config.py
        vyvod=$( ( set -x; run_scan "$S/repo" all "" "$S/src.yaml" ) 2>&1 || true )
        rm -rf "$S"
        printf %s "$vyvod" | grep -qF 'СЕКРЕТ? config.py:1:' || { echo "  старая ветка не сработала — проба слепа"; ok=1; }
        printf %s "$vyvod" | grep -qF 'ЗНАЧЕНИЕ ИЗ ИСТОЧНИКА' || { echo "  новая ветка не сработала — проба слепа"; ok=1; }
        ! printf %s "$vyvod" | grep -qF "$znach" || { echo "  значение попало в трассировку"; ok=1; }
        return "$ok"
    }

    # ── дыры ревью: источники значений, staged, сбой git ────────────────────
    REAL_GIT=$(command -v git)
    mkdir "$C/shim"
    # git-обёртка для проб: ломает или тормозит ТОЛЬКО поиск значений/шаблонов.
    cat > "$C/shim/git" <<'SHIM'
#!/usr/bin/env bash
args=" $* "
if [[ "$args" == *" grep "* ]]; then
    case "${FAIL_MODE:-}" in
        patterns) [[ "$args" == *" -inI "* ]] && exit 128 ;;
        values)   [[ "$args" == *" -f "* ]] && exit 128 ;;
        slow)     if [[ "$args" == *" -f "* ]]; then : > "$SLOW_FLAG"; sleep 2; fi ;;
    esac
fi
exec "$REAL_GIT" "$@"
SHIM
    chmod +x "$C/shim/git"
    printf -- '- вид: каталог\n  путь: SECRETS_DIR\n' > "$C/dir.yaml"

    stend_run() {  # $1 режим, $2 каталог ключей, $3 конфиг → kod, vyvod вызывающего
        kod=0; vyvod=$(run_scan "$S/repo" "$1" "$2" "$3" 2>&1) || kod=$?
    }
    stend_kommit() { git -C "$S/repo" -c user.email=a@b -c user.name=a commit -qm i >/dev/null; }

    # Случай 21. Спецсимволы в значении: значение ищется ДОСЛОВНО (-F), а не
    # как регулярное выражение. Больная половина — значение находится,
    # здоровая — текст, который регулярка приняла бы за совпадение, молчит.
    sluchai_spetssimvoly() {
        local S znach znach2 kod vyvod ok=0 istochnik
        for istochnik in env katalog; do
            stend_novyj; mkdir "$S/keys"
            znach="a,b./c*d[e\$f$(stend_znachenie)"; znach2="ab.cd$(stend_znachenie)"
            if [ "$istochnik" = env ]; then
                printf 'GAME_PASSWORD=%s\nADMIN_TOKEN=%s\n' "$znach" "$znach2" > "$S/.env"
                stend_konf "$S/src.yaml" "$S/.env"
            else
                printf '%s\n%s\n' "$znach" "$znach2" > "$S/keys/k"
                cp "$C/dir.yaml" "$S/src.yaml"
            fi
            printf 'сервер: %s\n' "$znach" > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
            stend_run all "$S/keys" "$S/src.yaml"
            [ "$kod" = 1 ] || { echo "  $istochnik: значение со спецсимволами не поймано (код $kod)"; ok=1; }
            ! printf %s "$vyvod" | grep -qF -- "$znach" || { echo "  $istochnik: ЗНАЧЕНИЕ НАПЕЧАТАНО"; ok=1; }
            printf 'сервер: a,bX/cccd ничего; %s\n' "${znach2/./X}" > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
            stend_run all "$S/keys" "$S/src.yaml"
            [ "$kod" = 0 ] || { echo "  $istochnik: текст, похожий как регулярка, покраснел (код $kod)"; ok=1; }
            rm -rf "$S"
        done
        return "$ok"
    }

    # Случай 22. Разбор .env: комментарий в конце строки, кавычки, хвост после
    # закрывающей кавычки, экранированная кавычка, имена PASS/AUTH/CRED/PW.
    sluchai_env_razbor() {
        local S znach kod vyvod ok=0 zapis fmt rfmt
        local -a zapisi=(
            'GAME_PASSWORD=%s # пароль игры|%s'
            'GAME_PASSWORD="%s" # пароль|%s'
            "GAME_PASSWORD='%s'   # c|%s"
            'GAME_PASSWORD="ab\\"cd%s"|ab"cd%s'
            'SERVER_PASS=%s|%s'
            'ADMIN_AUTH=%s|%s'
            'DB_CRED=%s|%s'
            'ADMIN_PW=%s|%s'
        )
        for zapis in "${zapisi[@]}"; do
            fmt="${zapis%%|*}"; rfmt="${zapis#*|}"
            stend_novyj; znach=$(stend_znachenie)
            # shellcheck disable=SC2059
            printf "$fmt\n" "$znach" > "$S/.env"
            stend_konf "$S/src.yaml" "$S/.env"
            # shellcheck disable=SC2059
            printf "x $rfmt y\n" "$znach" > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
            stend_run all "" "$S/src.yaml"
            [ "$kod" = 1 ] && printf %s "$vyvod" | grep -qF 'ЗНАЧЕНИЕ ИЗ ИСТОЧНИКА' \
                || { echo "  строка .env «$fmt» не разобрана (код $kod)"; ok=1; }
            ! printf %s "$vyvod" | grep -qF -- "$znach" || { echo "  «$fmt»: ЗНАЧЕНИЕ НАПЕЧАТАНО"; ok=1; }
            rm -rf "$S"
        done
        return "$ok"
    }

    # Случай 23. --staged видит ИЗМЕНЁННЫЙ (M), ПЕРЕИМЕНОВАННЫЙ (R) файл и
    # смену типа (T): фильтр «только добавленные/копии» пропускал их.
    sluchai_staged_status() {
        local S znach kod vyvod ok=0 bukva
        for bukva in M R T; do
            stend_novyj; znach=$(stend_znachenie)
            printf 'GAME_PASSWORD=%s\n' "$znach" > "$S/.env"; stend_konf "$S/src.yaml" "$S/.env"
            case "$bukva" in
                M) echo a > "$S/repo/f.md"; git -C "$S/repo" add f.md; stend_kommit
                   printf 'x %s y\n' "$znach" >> "$S/repo/f.md" ;;
                R) seq 1 40 > "$S/repo/old.md"; git -C "$S/repo" add old.md; stend_kommit
                   git -C "$S/repo" mv old.md new.md; printf 'x %s y\n' "$znach" >> "$S/repo/new.md" ;;
                T) ln -s /etc/hosts "$S/repo/t.md"; git -C "$S/repo" add t.md; stend_kommit
                   rm "$S/repo/t.md"; printf 'x %s y\n' "$znach" > "$S/repo/t.md" ;;
            esac
            git -C "$S/repo" add -A
            [ "$(git -C "$S/repo" diff --cached --name-status | cut -c1)" = "$bukva" ] \
                || { echo "  стенд $bukva: статус в индексе не тот — проба слепа"; ok=1; }
            stend_run staged "" "$S/src.yaml"
            [ "$kod" = 1 ] || { echo "  статус $bukva: значение в staged не поймано (код $kod)"; ok=1; }
            rm -rf "$S"
        done
        return "$ok"
    }

    # Случай 24. Имена с кавычкой, обратной чертой, табуляцией, скобками и
    # префиксом «:(» — git экранирует или трактует их как шаблон пути.
    sluchai_staged_strannye_imena() {
        local S znach kod vyvod ok=0 imya
        for imya in 'a"b.md' 'a\b.md' $'tab\tname.md' '[ab].md' ':(exclude)z.md'; do
            stend_novyj; znach=$(stend_znachenie)
            printf 'GAME_PASSWORD=%s\n' "$znach" > "$S/.env"; stend_konf "$S/src.yaml" "$S/.env"
            printf 'x %s y\n' "$znach" > "$S/repo/$imya"; git -C "$S/repo" add -A
            stend_run staged "" "$S/src.yaml"
            [ "$kod" = 1 ] || { echo "  имя «$imya»: значение в staged не поймано (код $kod)"; ok=1; }
            rm -rf "$S"
        done
        # Здоровая половина: имя со скобками — не шаблон пути. Значение лежит в
        # УЧТЁННОМ файле x1.md, который этим коммитом не меняется; шаблон «x[1].md»
        # зацепил бы его и покрасил чистый коммит.
        stend_novyj; znach=$(stend_znachenie)
        printf 'GAME_PASSWORD=%s\n' "$znach" > "$S/.env"; stend_konf "$S/src.yaml" "$S/.env"
        printf 'x %s y\n' "$znach" > "$S/repo/x1.md"; git -C "$S/repo" add x1.md; stend_kommit
        printf 'чисто\n' > "$S/repo/x[1].md"; git -C "$S/repo" add 'x[1].md'
        stend_run staged "" "$S/src.yaml"
        [ "$kod" = 0 ] || { echo "  имя «x[1].md» принято за шаблон пути (код $kod)"; ok=1; }
        rm -rf "$S"
        return "$ok"
    }

    # Случай 25. Файлы ключей без перевода строки не склеиваются в одну строку.
    sluchai_kljuchi_bez_perevoda() {
        local S v1 v2 v3 kod vyvod ok=0 imya
        stend_novyj; mkdir "$S/keys"
        v1=$(stend_znachenie); v2=$(stend_znachenie); v3=$(stend_znachenie)
        printf '%s' "$v1" > "$S/keys/a"; printf '%s' "$v2" > "$S/keys/b"; printf '%s' "$v3" > "$S/keys/c"
        for imya in "$v1" "$v2" "$v3"; do
            printf 'x %s y\n' "$imya" > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
            stend_run staged "$S/keys" "$C/dir.yaml"
            [ "$kod" = 1 ] || { echo "  ключ без \\n среди склеенных не пойман (код $kod)"; ok=1; }
        done
        rm -rf "$S"; return "$ok"
    }

    # Случай 26. Сбой git — красный, а не «чисто»: испорченный индекс и сбой
    # поиска в каждой из трёх веток (шаблоны, .env, каталог ключей).
    sluchai_sboj_git() {
        local S znach kod vyvod ok=0 vetka
        for vetka in indeks patterns env katalog; do
            stend_novyj; mkdir "$S/keys"; znach=$(stend_znachenie)
            printf 'GAME_PASSWORD=%s\n' "$znach" > "$S/.env"; printf '%s\n' "$znach" > "$S/keys/k"
            printf -- '- вид: env\n  путь: %s\n- вид: каталог\n  путь: SECRETS_DIR\n' "$S/.env" > "$S/src.yaml"
            printf 'ничего\n' > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
            case "$vetka" in
                indeks) printf 'garbage' > "$S/repo/.git/index"; stend_run all "$S/keys" "$S/src.yaml" ;;
                *) [ "$vetka" = patterns ] && FAIL_MODE=patterns || FAIL_MODE=values
                   kod=0; vyvod=$(PATH="$C/shim:$PATH" REAL_GIT="$REAL_GIT" FAIL_MODE="$FAIL_MODE" \
                       run_scan "$S/repo" all "$S/keys" "$S/src.yaml" 2>&1) || kod=$? ;;
            esac
            [ "$kod" = 1 ] || { echo "  сбой git ($vetka) дал код $kod, а не красный 1"; ok=1; }
            printf %s "$vyvod" | grep -qF 'git grep' || { echo "  сбой git ($vetka): нет слова о поиске в выводе"; ok=1; }
            rm -rf "$S"
        done
        return "$ok"
    }

    # Случай 27. Временные файлы со значением не остаются ни после обычного
    # прогона, ни после SIGTERM посреди поиска.
    sluchai_tmp_ne_ostayotsya() {
        local S X pid i ok=0
        stend_novyj; X=$(mktemp -d); mkdir -p "$X/scripts" "$X/config" "$X/tmp"
        cp "${BASH_SOURCE[0]}" "$X/scripts/check-secrets.sh"
        printf 'GAME_PASSWORD=%s\n' "$(stend_znachenie)" > "$S/.env"
        printf -- '- вид: env\n  путь: %s\n' "$S/.env" > "$X/config/secret-sources.yaml"
        echo x > "$S/repo/f.txt"; git -C "$S/repo" add f.txt
        TMPDIR="$X/tmp" bash "$X/scripts/check-secrets.sh" --staged "$S/repo" >/dev/null 2>&1 || true
        [ -z "$(ls -A "$X/tmp")" ] || { echo "  после обычного прогона остались файлы"; ok=1; }
        PATH="$C/shim:$PATH" REAL_GIT="$REAL_GIT" FAIL_MODE=slow SLOW_FLAG="$X/flag" TMPDIR="$X/tmp" \
            setsid bash "$X/scripts/check-secrets.sh" --staged "$S/repo" >/dev/null 2>&1 &
        pid=$!
        for i in $(seq 1 50); do [ -e "$X/flag" ] && break; sleep 0.1; done
        [ -e "$X/flag" ] || { echo "  прогон не дошёл до поиска значений — проба слепа"; ok=1; }
        [ -n "$(ls -A "$X/tmp")" ] || { echo "  посреди прогона временных файлов нет — проба слепа"; ok=1; }
        kill -TERM -- "-$pid" 2>/dev/null || { echo "  не удалось послать SIGTERM группе — проба слепа"; ok=1; }
        wait "$pid" 2>/dev/null || true
        # Подоболочка убирается чуть позже головного процесса: ждём до 3 секунд.
        for i in $(seq 1 30); do [ -z "$(ls -A "$X/tmp")" ] && break; sleep 0.1; done
        [ -z "$(ls -A "$X/tmp")" ] || { echo "  после SIGTERM остались временные файлы"; ok=1; }
        rm -rf "$S" "$X"; return "$ok"
    }

    # Случай 28. Каталог ключей: подкаталоги, симлинки, JSON и key: value,
    # токен Telegram целой строкой, нечитаемый файл, пустой каталог, отсутствие.
    sluchai_katalog_formy() {
        local S v kod vyvod ok=0
        proverit() {  # $1 имя, $2 значение, стенд уже заполнен
            printf 'пометка: %s  # не-секрет\n' "$2" > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
            stend_run all "$S/keys" "$C/dir.yaml"
            { [ "$kod" = 1 ] && printf %s "$vyvod" | grep -qF 'ЖИВОЙ КЛЮЧ В ФАЙЛЕ'; } || { echo "  $1: не пойман (код $kod)"; ok=1; }
        }
        stend_novyj; mkdir -p "$S/keys/a/b"; v=$(stend_znachenie); printf '%s\n' "$v" > "$S/keys/a/b/k"; proverit "подкаталог" "$v"; rm -rf "$S"
        stend_novyj; mkdir "$S/keys"; v=$(stend_znachenie); printf '%s\n' "$v" > "$S/real"; ln -s "$S/real" "$S/keys/k"; proverit "симлинк" "$v"; rm -rf "$S"
        stend_novyj; mkdir "$S/keys"; v=$(stend_znachenie); printf '{"token": "%s", "n": 1}\n' "$v" > "$S/keys/c.json"; proverit "JSON" "$v"; rm -rf "$S"
        stend_novyj; mkdir "$S/keys"; v=$(stend_znachenie); printf 'token: %s\n' "$v" > "$S/keys/c.yaml"; proverit "key: value" "$v"; rm -rf "$S"
        stend_novyj; mkdir "$S/keys"; v="123456789:AA$(printf %s "$(stend_znachenie)$(stend_znachenie)" | cut -c1-33)"
        printf '%s\n' "$v" > "$S/keys/tg"; proverit "токен Telegram с двоеточием" "$v"; rm -rf "$S"
        # адрес почты из JSON ключа — не секрет: текст с ним не краснеет
        stend_novyj; mkdir "$S/keys"; v=$(stend_znachenie)
        printf '{\n  "client_email": "svc-robot-account@proj.iam.example.com",\n  "token": "%s"\n}\n' "$v" > "$S/keys/c.json"
        printf 'аккаунт svc-robot-account@proj.iam.example.com\n' > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
        stend_run all "$S/keys" "$C/dir.yaml"
        [ "$kod" = 0 ] || { echo "  адрес почты из JSON принят за ключ (код $kod)"; ok=1; }
        rm -rf "$S"
        # нечитаемый файл ключа — предупреждение (от root файл читаем: не судится)
        stend_novyj; mkdir "$S/keys"; printf '%s\n' "$(stend_znachenie)" > "$S/keys/ok"
        printf '%s\n' "$(stend_znachenie)" > "$S/keys/locked"; chmod 000 "$S/keys/locked"
        if [ -r "$S/keys/locked" ]; then echo "  нечитаемый файл читаем (запуск от root) — не судится"
        else
            echo 'ничего' > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
            stend_run all "$S/keys" "$C/dir.yaml"
            { [ "$kod" = 0 ] && printf %s "$vyvod" | grep -qF 'нечитаем'; } || { echo "  нечитаемый файл ключа прошёл молча (код $kod)"; ok=1; }
        fi
        chmod 600 "$S/keys/locked"; rm -rf "$S"
        # каталог есть, годных значений нет — предупреждение, не тишина
        stend_novyj; mkdir "$S/keys"; echo short > "$S/keys/s"; echo 'ничего' > "$S/repo/notes.txt"; git -C "$S/repo" add notes.txt
        stend_run all "$S/keys" "$C/dir.yaml"
        { [ "$kod" = 0 ] && printf %s "$vyvod" | grep -qF 'ПРЕДУПРЕЖДЕНИЕ'; } || { echo "  пустой каталог ключей прошёл молча (код $kod)"; ok=1; }
        # каталога нет — тихо, так задумано
        stend_run all "$S/net-takogo" "$C/dir.yaml"
        { [ "$kod" = 0 ] && [ "$vyvod" = "check-secrets: чисто" ]; } || { echo "  отсутствующий каталог: код $kod, вывод: $vyvod"; ok=1; }
        rm -rf "$S"; return "$ok"
    }

    # Случай 29. Числовой лимит у имени с TOKENS — не секрет; настоящий токен рядом и
    # одиночный TOKEN с числом — красные.
    sluchai_chislovoj_limit() {
        printf "CTX_WINDOW_TOKENS=800000\nMAX_TOKEN_LIMIT=5000\n" > "$T/lim.env"  # не-секрет
        printf "ADMIN_TOKEN=a9f3k2x7q1\nAPI_TOKEN=123456789\nDB_PASSWORD=123456 # password_len=6\n" >> "$T/lim.env"  # не-секрет: стенд самотеста
        git -C "$T" add lim.env >/dev/null 2>&1
        local vyvod; vyvod=$(run_scan "$T" all "" "$KONF_PUSTOJ" 2>&1 || true)
        rm -f "$T/lim.env"; git -C "$T" rm -q --cached lim.env
        if printf %s "$vyvod" | grep -qE 'lim.env:[12]:'; then
            echo "  числовой лимит принят за секрет"; return 1
        fi
        printf %s "$vyvod" | grep -q 'lim.env:3:' || { echo "  отсев лимитов проглотил буквенно-цифровой токен"; return 1; }
        printf %s "$vyvod" | grep -q 'lim.env:4:' || { echo "  отсев лимитов проглотил TOKEN с числом"; return 1; }
        printf %s "$vyvod" | grep -q 'lim.env:5:' || { echo "  хвост строки погасил настоящий пароль"; return 1; }
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
        "значение из .env в staged-файле — красный, без значения в выводе:sluchai_znachenie_iz_istochnika"
        "значения из .env нет в файлах — зелёный:sluchai_istochnik_zdorovyj"
        "источник нечитаем — предупреждение:sluchai_istochnik_nechitaem"
        "источника нет — тихо или с предупреждением по обязательности:sluchai_istochnika_net"
        "значение короче 6 — пропущено:sluchai_korotkoe_znachenie"
        "ключ без PASSWORD/TOKEN/SECRET/KEY не берётся:sluchai_kljuch_bez_slova"
        "конфиг источников не найден — предупреждение:sluchai_konfig_ne_najden"
        "маркер не-секрет не гасит точное значение:sluchai_marker_ne_gasit_znachenie"
        "источник без подходящих ключей — «проверять нечего»:sluchai_istochnik_bez_klyuchej"
        "под set -x значение не в трассировке:sluchai_xtrace_bez_znacheniya"
        "спецсимволы в значении ищутся дословно:sluchai_spetssimvoly"
        "разбор .env: комментарий, кавычки, экранирование, имена PASS/AUTH/CRED/PW:sluchai_env_razbor"
        "staged видит изменённый, переименованный и сменивший тип файл:sluchai_staged_status"
        "staged: странные имена файлов:sluchai_staged_strannye_imena"
        "файлы ключей без перевода строки не склеиваются:sluchai_kljuchi_bez_perevoda"
        "сбой git — красный во всех ветках:sluchai_sboj_git"
        "временные файлы не остаются после прогона и SIGTERM:sluchai_tmp_ne_ostayotsya"
        "каталог ключей: глубина, симлинки, JSON, Telegram, нечитаемый, пустой:sluchai_katalog_formy"
        "числовой лимит TOKENS не секрет, токены рядом — красные:sluchai_chislovoj_limit"
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
