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
)
MARKER='не-секрет'

run_scan() {  # $1 = каталог репо, $2 = режим all|staged
    local repo="$1" mode="$2" fail=0 files hits f
    git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 \
        || { echo "check-secrets: $repo — не git-репозиторий, сканировать нечего"; return 2; }
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
             "${PATTERNS[@]}" "${SCOPE[@]}" 2>/dev/null | grep -v "$MARKER" || true)
    if [ -n "$hits" ]; then
        printf '%s\n' "$hits" | sed 's|^|СЕКРЕТ? |'
        fail=1
    fi
    if [ "$fail" = 1 ]; then
        echo "check-secrets: ПОХОЖЕ НА СЕКРЕТ В GIT — убери значение в \$SECRETS_DIR, в код клади путь/имя переменной (ложное срабатывание гасится маркером «$MARKER» в строке)"
        return 1
    fi
    echo "check-secrets: чисто"
    return 0
}

# ── самотест: больной и здоровый случай во временном репо ───────────────────
if [ "${1:-}" = "--selftest" ]; then
    T=$(mktemp -d)
    trap 'rm -rf "$T"' EXIT
    git -C "$T" init -q

    # Больной случай: токен в коде обязан дать красный.  # не-секрет
    echo 'token="test123"' > "$T/config.py"  # не-секрет
    git -C "$T" add config.py
    if run_scan "$T" all >/dev/null; then
        echo "SELFTEST FAIL: токен test123 не пойман"; exit 1
    fi
    echo "selftest 1/4: больной случай в кавычках пойман (красный) — OK"
    rm "$T/config.py"
    git -C "$T" rm -q --cached config.py

    # Больной случай БЕЗ кавычек (.env/ini-стиль) — раньше проскакивал.  # не-секрет
    echo 'password = supersecret123' > "$T/app.ini"  # не-секрет
    git -C "$T" add app.ini
    if run_scan "$T" all >/dev/null; then
        echo "SELFTEST FAIL: безкавычечный секрет не пойман"; exit 1
    fi
    echo "selftest 2/4: безкавычечный больной случай пойман (красный) — OK"
    rm "$T/app.ini"
    git -C "$T" rm -q --cached app.ini

    # Здоровый случай: чистый репо обязан пройти.
    echo 'db_password_file = "/etc/x/secrets/db_password"' > "$T/clean.py"
    git -C "$T" add clean.py
    if ! run_scan "$T" all >/dev/null; then
        echo "SELFTEST FAIL: чистый репо покраснел"; exit 1
    fi
    echo "selftest 3/4: здоровый случай прошёл (зелёный) — OK"

    # Режим --staged: секрет лежит В ИНДЕКСЕ, рабочее дерево уже чистое —
    # коммитится индекс, краснеть обязан именно он.  # не-секрет
    echo 'api_key="AB12cd34ef56"' > "$T/staged.py"  # не-секрет
    git -C "$T" add staged.py
    echo '# чисто' > "$T/staged.py"
    if run_scan "$T" staged >/dev/null; then
        echo "SELFTEST FAIL: --staged не увидел секрет в индексе"; exit 1
    fi
    echo "selftest 4/4: --staged ловит секрет из индекса (красный) — OK"
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
    source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
    REPO="$(konf_iz_fajla PROJECT_DIR)"
    REPO="${REPO:-$PWD}"
    # Подмена обязана быть ВИДНА, а не молчать.
    [ -n "${PROJECT_DIR:-}" ] && [ "${PROJECT_DIR}" != "$REPO" ] && \
        echo "check-secrets: сканирую $REPO (из паспорта); PROJECT_DIR в окружении — $PROJECT_DIR, он не в счёт"
fi
run_scan "$REPO" "$MODE"
