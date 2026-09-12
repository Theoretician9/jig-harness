#!/usr/bin/env bash
#
# publikaciya.sh — перенести ПРОВЕРЕННОЕ в публичную ветку харнеса.
#
# Задача владельца 11.09.2026: «мне нужно чтобы было две версии гита — ту
# которая проверена и я ее публично публикую, и та, которую я доделываю».
#
# Отсюда смысл двери: публичная ветка показывает людям то, что владелец
# ПРОВЕРИЛ. Поэтому здесь три замка, и каждый из них — про это:
#   1) ворота харнеса зелёные (иначе публикуем заведомо больное);
#   2) согласие владельца с предметом ПУБЛИКАЦИИ — не «выкатывай, да» и не
#      «на гит отправляй, да»: одно «да» — одно действие (правило владельца);
#   3) каждая задача, попадающая в публичную ветку, в dev-map.yaml имеет
#      статус `done` (проверено владельцем) либо `owner_testable: false`.
#      Это И-4 на витрине: непроверенное не выдаётся за проверенное.
#
# Имена веток — ДАННЫЕ (harness.conf): PUBLIC_BRANCH и WORK_BRANCH. Своих
# умолчаний нет — схему выбирает владелец, а не код.
#
# Запуск:
#   bash scripts/publikaciya.sh            # проверить и опубликовать
#   bash scripts/publikaciya.sh --проверка # только проверить, ничего не менять
#   bash scripts/publikaciya.sh --selftest
#
# Код возврата: 0 — опубликовано (или проверка прошла), 1 — отказ замка,
# 2 — не настроено.
set -uo pipefail

INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
# Конфиги грузятся так, чтобы ОКРУЖЕНИЕ было старше файла: иначе проба с
# подставным путём судит БОЕВУЮ установку (улика 12.09.2026 — команда смены
# модели ушла в рабочую панель агента).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
konf_zagruzit PROJECT_DIR LOG_DIR TMUX_SESSION SECRETS_DIR AGENT_START_CMD

PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
# Имя агента — из паспорта: вшитое сюда, оно след НАШЕЙ установки.
PACKAGE_DIR="${STARTER_DIR:-/home/$(konf_iz_fajla AGENT_USER)/starter/СТАРТОВЫЙ-ПАКЕТ}"

say() { printf '%s\n' "$*"; }

# ── Задачи, попадающие на витрину, обязаны быть ПРОВЕРЕНЫ владельцем ────────
# Трейлер «Dev-Map: <id>» стоит в коммитах ПРОЕКТА, а публикуется ПАКЕТ: они
# коммитятся парой, и связать их можно только временем. Поэтому спрашиваем:
# какие задачи появились в проекте ПОСЛЕ момента, на котором стоит публичная
# ветка. Читать историю пакета бесполезно — трейлеров там ноль, и замок
# пропускал бы всё и всегда (замер 11.09.2026, поймано до включения).
непроверенные_с() { # $1=момент ISO → печатает id задач не в статусе done
    local since="$1" ids
    local ogr=()
    [ -n "$since" ] && ogr=(--since="$since")
    ids=$(git -C "$PROJECT_DIR" log "${ogr[@]}" --format=%B 2>/dev/null \
          | sed -n 's/^Dev-Map: *//p' | sort -u) || return 0
    [ -n "$ids" ] || return 0
    DEVMAP="$PROJECT_DIR/dev-map.yaml" \
    DEVMAP_ARCHIVE="$PROJECT_DIR/docs/dev-map-archive.yaml" \
    IDS="$ids" python3 - <<'PY'
import os, sys
try:
    import yaml
except ImportError:
    # Без PyYAML замок не может судить — молчать нельзя, отказ честнее.
    print("НЕТ-PyYAML")
    sys.exit(0)

# Карта И архив: задачи уезжают в архив по мере роста карты, их статус от
# этого не меняется. Искать только в карте — получать «в карте нет» на всё
# старое и отказывать зря (замер 11.09.2026).
карты = [yaml.safe_load(open(os.environ["DEVMAP"], encoding="utf-8"))]
архив = os.environ.get("DEVMAP_ARCHIVE")
if архив and os.path.exists(архив):
    карты.append(yaml.safe_load(open(архив, encoding="utf-8")))

def задачи(узел):
    if isinstance(узел, dict):
        if "id" in узел and "status" in узел:
            yield узел
        for значение in узел.values():
            yield from задачи(значение)
    elif isinstance(узел, list):
        for значение in узел:
            yield from задачи(значение)

по_id = {з["id"]: з for карта in карты for з in задачи(карта)}
for ид in os.environ["IDS"].split():
    з = по_id.get(ид)
    if з is None:
        print(f"{ид} (в карте нет)")
    elif з.get("status") != "done" and з.get("owner_testable") is not False:
        print(f"{ид} (статус {з.get('status')} — ждёт проверки владельцем)")
PY
}

# ── Замки 4-6 и публикация собранного дерева ────────────────────────────────
# Наружу уезжает не пакет, а СБОРКА: инструмент без нашей мастерской. Порядок
# замков — единственное, что здесь своё; сборка, поиск секретов и поиск следов
# зовутся готовыми.
опубликовать_сборку() {
    local sborka="$PUBLIC_BUILD_DIR" paket="$PACKAGE_DIR" itog hesh

    # Замок 4: собрать из УЧТЁННОГО состояния пакета.
    itog=$(python3 "$PROJECT_DIR/scripts/sobrat-publichnoe.py" \
                   --пакет "$paket" --куда "$sborka" 2>&1) || {
        say "ОТКАЗ: сборка не прошла — $itog"; return 1; }
    hesh=$(printf '%s' "$itog" | python3 -c 'import json,sys; print(json.load(sys.stdin)["пакет"])' 2>/dev/null)
    say "замок 4 · собрано: $itog"

    # Своя история у сборки: публичная ветка ведётся здесь, а не в пакете.
    if [ ! -d "$sborka/.git" ]; then
        git -C "$sborka" init -q -b "$PUBLIC_BRANCH"
        git -C "$sborka" remote add "$PUBLIC_REMOTE" \
            "$(git -C "$paket" remote get-url "$PUBLIC_REMOTE")"
    fi
    git -C "$sborka" fetch --quiet "$PUBLIC_REMOTE" 2>/dev/null || true
    git -C "$sborka" add -A

    # Замок 5 (И-3): у нового репозитория своих хуков нет, и сторож секретов
    # его не сторожит — зовём явно, по временному индексу.
    local vyvod
    if ! vyvod=$(bash "$PROJECT_DIR/scripts/check-secrets.sh" "$sborka" 2>&1); then
        say "ОТКАЗ: в сборке похожее на секрет — публикация отменена"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
    say "замок 5 · секретов в сборке нет"

    # Замок 6: следы установки. Образцы берутся не из данных сборщика.
    if ! vyvod=$(python3 "$PROJECT_DIR/scripts/check-publichnoe-chisto.py" "$sborka" \
                         --пакет "$paket" --проект "$PROJECT_DIR" 2>&1); then
        say "ОТКАЗ: в сборке следы этой установки — публикация отменена"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
    say "замок 6 · следов установки в сборке нет"

    if git -C "$sborka" diff --cached --quiet; then
        say "публиковать нечего: сборка не отличается от опубликованного"
        return 0
    fi
    git -C "$sborka" -c user.name="$PUBLIC_COMMIT_NAME" \
        -c user.email="$PUBLIC_COMMIT_EMAIL" \
        commit -q -m "версия от $(date +%F), пакет $hesh" || {
        say "ОТКАЗ: коммит сборки не прошёл"; return 1; }

    # Только вперёд. Публичную историю переписывает владелец руками и по своему
    # слову: у тех, кто уже склонировал, обновление держится на ff-only, и
    # force сломал бы его навсегда.
    if git -C "$sborka" rev-parse --verify -q "$PUBLIC_REMOTE/$PUBLIC_BRANCH" >/dev/null; then
        if ! git -C "$sborka" merge-base --is-ancestor \
               "$PUBLIC_REMOTE/$PUBLIC_BRANCH" HEAD; then
            say "ОТКАЗ: расхождение с публичной веткой — в неё писали помимо сборки."
            say "Разбирает человек: force здесь запрещён, он ломает обновление у всех, кто склонировал."
            return 1
        fi
    fi
    if ! git -C "$sborka" push --quiet "$PUBLIC_REMOTE" "HEAD:$PUBLIC_BRANCH" 2>&1; then
        say "ОТКАЗ: push в $PUBLIC_BRANCH не прошёл — разберись руками"
        return 1
    fi
    say "опубликовано: $PUBLIC_BRANCH ← сборка из пакета $hesh"
    return 0
}

if [ "${1:-}" = "--selftest" ]; then
    ok=1
    # БОЛЬНОЙ СЛУЧАЙ первым: несуществующая ветка обязана дать отказ, а не
    # молчаливый успех — «опубликовано» без публикации хуже, чем отказ.
    # Пустые переменные окружения проба создать НЕ может: скрипт читает
    # harness.conf и перезаписывает их. Условие создаётся пустым конфигом —
    # иначе проба судит машину, а не код (она молча падала до 11.09.2026).
    # Паспорт задан, а ветки пусты: условие пробы — ИМЕННО незаданные ветки.
    # Прежде проба подавала пустой конфиг целиком, и после снятия умолчания
    # пути скрипт честно отказывал раньше — про PROJECT_DIR (12.09.2026).
    PUSTOJ_KONFIG=$(mktemp)
    printf 'PROJECT_DIR="%s"\nPUBLIC_BRANCH=""\n' "$(mktemp -d)" > "$PUSTOJ_KONFIG"
    OUT=$(HARNESS_CONF="$PUSTOJ_KONFIG" HARNESS_INSTALL_CONF="$PUSTOJ_KONFIG" \
          bash "$0" --проверка 2>&1); RC=$?
    if [ "$RC" = 2 ] && printf '%s' "$OUT" | grep -q "PUBLIC_BRANCH"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: ветки не заданы — отказ с внятной причиной"
    else
        echo "  ПЛОХО ветки не заданы: код $RC, ответ: ${OUT:0:120}"; ok=0
    fi
    # БОЛЬНОЙ СЛУЧАЙ: паспорта нет вовсе. Прежде здесь стоял наш путь
    # умолчанием — скрипт брался публиковать ЧУЖУЮ установку как свою.
    : > "$PUSTOJ_KONFIG"
    OUT=$(HARNESS_CONF="$PUSTOJ_KONFIG" HARNESS_INSTALL_CONF="$PUSTOJ_KONFIG" \
          bash "$0" --проверка 2>&1); RC=$?
    rm -f "$PUSTOJ_KONFIG"
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q "PROJECT_DIR"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: без паспорта отказ вслух, а не чужой каталог"
    else
        echo "  ПЛОХО без паспорта: код $RC, ответ: ${OUT:0:120}"; ok=0
    fi
    # БОЛЬНОЙ СЛУЧАЙ 2: сегодняшние задачи лежат в статусе test — замок обязан
    # их назвать. Прежняя редакция читала историю ПАКЕТА, где трейлеров нет, и
    # молча отвечала «всё проверено».
    NAMED_TODAY=$(непроверенные_с "$(date -d '1 day ago' +%Y-%m-%dT%H:%M:%S)")
    if [ -n "$NAMED_TODAY" ]; then
        echo "  ок    непроверенные задачи суток названы: $(printf '%s' "$NAMED_TODAY" | head -1)"
    else
        echo "  ПЛОХО замок не назвал ни одной непроверенной задачи за сутки — он читает не ту историю"; ok=0
    fi
    # Далёкое будущее: задач нет — и замок обязан молчать, а не выдумывать.
    if [ -z "$(непроверенные_с "$(date -d '1 day' +%Y-%m-%dT%H:%M:%S)")" ]; then
        echo "  ок    задач после момента нет — замок молчит"
    else
        echo "  ПЛОХО замок нашёл задачи в будущем"; ok=0
    fi
    # ── пути публикации СБОРКИ: подставной пакет и подставной удалённый ──
    # Улика «push не звался» — состояние удалённого репозитория, а не
    # подставной git в PATH: подставной git сломал бы и саму сборку, и проба
    # судила бы свою заглушку вместо кода (правило смены 12.09.2026).
    T=$(mktemp -d)
    proba_dvor() {  # готовит подставные пакет, удалённый и конфиги; печатает каталог
        local dvor="$1"
        mkdir -p "$dvor/пакет/харнес/config" "$dvor/пакет/харнес/память" \
                 "$dvor/пакет/харнес/шаблоны-задач" "$dvor/сборка"
        printf '# харнес\n@STATE.md\n' > "$dvor/пакет/харнес/CLAUDE.md"
        printf 'состояние\n' > "$dvor/пакет/харнес/STATE.md"
        printf '# ТОН\nзамер речи\n' > "$dvor/пакет/харнес/память/ТОН.md"
        printf '# ТОН\n\n<!-- потолки: мягкий=700 жёсткий=1500 -->\n' \
            > "$dvor/пакет/харнес/шаблоны-задач/ТОН-заготовка.md"
        printf 'исключить: []\nзаменить_шаблоном:\n  харнес/память/ТОН.md: харнес/шаблоны-задач/ТОН-заготовка.md\nобезличить_ключи: [PROJECT_DIR]\nоставить: [харнес/CLAUDE.md]\n' \
            > "$dvor/пакет/харнес/config/публичная-сборка.yaml"
        git -C "$dvor/пакет" init -q -b master
        git -C "$dvor/пакет" add -A
        git -C "$dvor/пакет" -c user.name=t -c user.email=t@t commit -qm проба
        git init -q --bare "$dvor/удалённый.git"
        git -C "$dvor/пакет" remote add jig "$dvor/удалённый.git"
        printf 'PROJECT_DIR="%s"\nAGENT_USER="подставной"\nLOG_DIR="%s"\n' \
            "$PROJECT_DIR" "$dvor" > "$dvor/паспорт.conf"
        printf 'PUBLIC_REMOTE="jig"\nPUBLIC_BRANCH="master"\nPUBLIC_BUILD_DIR="%s"\nPUBLIC_COMMIT_NAME="проба-харнес"\nPUBLIC_COMMIT_EMAIL="proba@harness.local"\n' \
            "$dvor/сборка" > "$dvor/харнес.conf"
    }
    proba_publ() {  # запускает публикацию сборки в подставном дворе
        local dvor="$1"
        PACKAGE_DIR="$dvor/пакет" PUBLIC_BUILD_DIR="$dvor/сборка" \
        PUBLIC_REMOTE=jig PUBLIC_BRANCH=master \
        PUBLIC_COMMIT_NAME="проба-харнес" PUBLIC_COMMIT_EMAIL="proba@harness.local" \
        PROJECT_DIR="$PROJECT_DIR" опубликовать_сборку 2>&1
    }
    kommitov() { git -C "$1" rev-list --count master 2>/dev/null || echo 0; }

    # БОЛЬНОЙ: след установки в коде сборки — отказ ДО push.
    proba_dvor "$T/чистота"
    printf 'ПУТЬ = "%s"\n' "$PROJECT_DIR" > "$T/чистота/пакет/харнес/след.py"
    git -C "$T/чистота/пакет" add -A
    git -C "$T/чистота/пакет" -c user.name=t -c user.email=t@t commit -qm след
    BYLO=$(kommitov "$T/чистота/удалённый.git")
    OUT=$(proba_publ "$T/чистота"); RC=$?
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q 'образец=PROJECT_DIR' \
       && [ "$(kommitov "$T/чистота/удалённый.git")" = "$BYLO" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: след установки — отказ, push не звался"
    else
        echo "  ПЛОХО след установки не остановил публикацию: код $RC, ${OUT:0:100}"; ok=0
    fi

    # БОЛЬНОЙ: похожее на токен в сборке — отказ замком секретов ДО коммита.
    proba_dvor "$T/секрет"
    PREFIKS="12345678"
    OBRAZEC='TOKEN = "%s:AA%s"'   # не-секрет: образец формы, собирается из кусков
    printf "$OBRAZEC\n" "$PREFIKS" "$(printf 'a%.0s' $(seq 33))" \
        > "$T/секрет/пакет/харнес/тайна.py"
    git -C "$T/секрет/пакет" add -A
    git -C "$T/секрет/пакет" -c user.name=t -c user.email=t@t commit -qm тайна
    OUT=$(proba_publ "$T/секрет"); RC=$?
    # Проба требует ПРИЧИНУ отказа, а не просто ненулевой код: «команды нет»
    # (127) выглядит как успешная защита и зеленит пробу впустую.
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -qi 'секрет' \
       && [ "$(kommitov "$T/секрет/удалённый.git")" = "0" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: похожее на секрет — отказ, ничего не уехало"
    else
        echo "  ПЛОХО секрет в сборке не остановил публикацию: код $RC, ${OUT:0:100}"; ok=0
    fi

    # Здоровый: чистый пакет — коммит своим автором, удалённый вырос на один.
    proba_dvor "$T/здоровый"
    OUT=$(proba_publ "$T/здоровый"); RC=$?
    SOOBSH=$(git -C "$T/здоровый/сборка" log -1 --format=%s 2>/dev/null)
    AVTOR=$(git -C "$T/здоровый/сборка" log -1 --format=%ae 2>/dev/null)
    HESH_PAKETA=$(git -C "$T/здоровый/пакет" rev-parse --short HEAD)
    if [ "$RC" = 0 ] && [ "$(kommitov "$T/здоровый/удалённый.git")" = "1" ] \
       && printf '%s' "$SOOBSH" | grep -q "^версия от" \
       && printf '%s' "$SOOBSH" | grep -q "$HESH_PAKETA" \
       && [ "$AVTOR" = "proba@harness.local" ]; then
        echo "  ок    здоровый: сборка опубликована, автор и хэш пакета в коммите"
    else
        echo "  ПЛОХО здоровая публикация не прошла: код $RC, «$SOOBSH», автор $AVTOR"; ok=0
    fi

    # БОЛЬНОЙ: удалённый ушёл вперёд чужим коммитом — отказ без force.
    CHUZHOJ=$(mktemp -d)
    git clone -q "$T/здоровый/удалённый.git" "$CHUZHOJ/копия"
    printf 'чужое\n' > "$CHUZHOJ/копия/чужое.txt"
    git -C "$CHUZHOJ/копия" add -A
    git -C "$CHUZHOJ/копия" -c user.name=ч -c user.email=ч@ч commit -qm чужое
    git -C "$CHUZHOJ/копия" push -q origin master
    BYLO=$(kommitov "$T/здоровый/удалённый.git")
    printf 'ещё\n' > "$T/здоровый/пакет/новое.txt"
    git -C "$T/здоровый/пакет" add -A
    git -C "$T/здоровый/пакет" -c user.name=t -c user.email=t@t commit -qm ещё
    OUT=$(proba_publ "$T/здоровый"); RC=$?
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -qi 'расхожд' \
       && [ "$(kommitov "$T/здоровый/удалённый.git")" = "$BYLO" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: расхождение веток — отказ, история не переписана"
    else
        echo "  ПЛОХО расхождение не остановило push: код $RC, ${OUT:0:100}"; ok=0
    fi
    rm -rf "$T" "$CHUZHOJ"

    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: 8 путей, первым — больной случай"; exit 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; exit 1
fi

# Код 2 = «не настроено», код 1 = «замок отказал». Разные вещи: первое чинит
# установка, второе — владелец. «${X:?}» даёт 1 на оба случая, поэтому явно.
if [ -z "${PUBLIC_BRANCH:-}" ] || [ -z "${PUBLIC_REMOTE:-}" ]    || [ -z "${PUBLIC_BUILD_DIR:-}" ] || [ -z "${PUBLIC_COMMIT_EMAIL:-}" ]; then
    echo "не настроено: в harness.conf пусты PUBLIC_REMOTE, PUBLIC_BRANCH, PUBLIC_BUILD_DIR и/или PUBLIC_COMMIT_EMAIL" >&2
    exit 2
fi

CHECK_ONLY=0
FIRST=0
[ "${1:-}" = "--проверка" ] && CHECK_ONLY=1
# Первая публикация — особый случай, и решает его владелец, а не скрипт.
# Публичной ветки ещё нет: отсекать нечего, и замок 3 судить не по чему.
# 11.09.2026 владелец сказал: «1 публикуй как есть, проверю — обновим если
# что-то не так». Флаг работает ТОЛЬКО при отсутствующей публичной ветке и
# называет число непроверенных задач вслух — молчаливого «опубликовано» тут
# быть не должно.
[ "${1:-}" = "--первая" ] && FIRST=1
# Прямое слово владельца перевешивает замок 3: публичная ветка — ЕГО витрина,
# и он вправе выложить непроверенное, зная состав. Честность держится не
# отказом, а оглаской: список непроверенных задач уходит ему в канал этим же
# скриптом. Молчаливой публикации непроверенного не бывает (И-4).
PO_SLOVU=0
[ "${1:-}" = "--по-слову" ] && PO_SLOVU=1

say "=== публикация: $WORK_BRANCH → $PUBLIC_BRANCH ==="

# ── Замок 1: ворота ────────────────────────────────────────────────────────
if ! bash "$PROJECT_DIR/scripts/vorota.sh" > /tmp/публикация-ворота.log 2>&1; then
    say "ОТКАЗ: ворота красные — публиковать нельзя. Хвост:"
    tail -5 /tmp/публикация-ворота.log
    exit 1
fi
say "замок 1 · ворота зелёные"

# ── Замок 2: согласие владельца ИМЕННО на публикацию ───────────────────────
REFUSAL=$(python3 "$PROJECT_DIR/scripts/deploy_guard.py" --действие публикация --только-согласие $([ "$CHECK_ONLY" = 1 ] && echo --не-тратить) 2>&1) || true
if [ -n "$REFUSAL" ] && printf '%s' "$REFUSAL" | grep -q "нет согласия"; then
    say "ОТКАЗ: $REFUSAL"
    say "Скажи владельцу, ЧТО именно публикуется, и дождись «да» про публикацию."
    exit 1
fi
say "замок 2 · согласие владельца на публикацию есть"

# ── Замок 3: на витрину — только проверенное владельцем ────────────────────
# Момент, на котором стоит публичная ветка. Нет ветки — нет и точки отсчёта:
# первую выбирает владелец, а не скрипт (вопрос задан 11.09.2026).
# Удалённый — ДАННЫЕ (PUBLIC_REMOTE в harness.conf). Вшитое имя уже стоило
# смены 11.09.2026: сверка версий искала «origin», живой пакет звал удалённый
# иначе. Здесь вшитое «harness» указывало на ПРИВАТНЫЙ репозиторий пакета —
# замок считал точку отсечки по нему, а публичная ветка живёт в другом.
# Точка отсчёта берётся ТАМ, где живёт публичная ветка. С переходом на сборку
# это каталог сборки, а не пакет: в пакете публичного удалённого может уже не
# быть, и замок 3 считал бы диапазон по пустоте.
OTKUDA_VETKA="$PACKAGE_DIR"
[ -d "${PUBLIC_BUILD_DIR:-}/.git" ] && OTKUDA_VETKA="$PUBLIC_BUILD_DIR"
git -C "$OTKUDA_VETKA" fetch "$PUBLIC_REMOTE" 2>/dev/null || true
SINCE=$(git -C "$OTKUDA_VETKA" log -1 --format=%cI "$PUBLIC_REMOTE/$PUBLIC_BRANCH" 2>/dev/null) || true
if [ -z "$SINCE" ] && [ "$FIRST" = 0 ]; then
    say "ОТКАЗ: публичной ветки «$PUBLIC_BRANCH» ещё нет — первую точку отсечки называет владелец"
    say "Владелец сказал публиковать как есть? Тогда: bash scripts/publikaciya.sh --первая"
    exit 1
fi
if [ -n "$SINCE" ] && [ "$FIRST" = 1 ]; then
    say "ОТКАЗ: публичная ветка «$PUBLIC_BRANCH» уже есть — --первая больше не про неё"
    exit 1
fi
if [ "$FIRST" = 1 ]; then
    # «--since=1970-01-01» git понимает как ПУСТОЙ диапазон (проверено: 0 против
    # 202 трейлеров без него) — счёт выходил нулевым и врал владельцу.
    CHISLO=$(непроверенные_с "" | grep -c . || true)
    say "ПЕРВАЯ ПУБЛИКАЦИЯ по слову владельца: уезжает как есть, задач без его"
    say "проверки — $CHISLO. Это записано; следующие публикации судит замок 3."
fi
# При первой публикации замку 3 судить не по чему: отсекать нечего, и число
# непроверенных уже названо вслух выше. Дальше он работает как обычно.
PENDING=""
[ "$FIRST" = 0 ] && PENDING=$(непроверенные_с "$SINCE")
if printf '%s' "$PENDING" | grep -q "НЕТ-PyYAML"; then
    say "ОТКАЗ: нет PyYAML — статусы задач проверить нечем (замок судить не может)"
    exit 2
fi
if [ -n "$PENDING" ] && [ "$PO_SLOVU" = 1 ]; then
    say "ПО СЛОВУ ВЛАДЕЛЬЦА: уезжает непроверенное им — $(printf '%s\n' "$PENDING" | grep -c .) задач(и):"
    printf '%s\n' "$PENDING" | sed 's/^/  • /'
    SPISOK=$(printf '%s\n' "$PENDING" | sed 's/ (статус.*//' | paste -sd', ')
    bash "$PROJECT_DIR/scripts/tg_send.sh" "Публикую по твоему слову. В публичную ветку уезжает непроверенное тобой: $SPISOK" >/dev/null 2>&1 || true
    PENDING=""
fi
if [ -n "$PENDING" ]; then
    say "ОТКАЗ: в публичную ветку попали бы задачи, которых владелец НЕ проверял:"
    printf '%s\n' "$PENDING" | sed 's/^/  • /'
    say "И-4: непроверенное не выдаётся за проверенное. Пусть владелец проверит и скажет — тогда статус станет done."
    exit 1
fi
if [ "$FIRST" = 1 ]; then
    say "замок 3 · первая публикация: не судит, число непроверенных названо выше"
else
    say "замок 3 · все задачи диапазона проверены владельцем"
fi

if [ "$CHECK_ONLY" = 1 ]; then
    say "проверка пройдена — публиковать можно (запуск без --проверка)"
    exit 0
fi

# ── Публикация: собранное дерево, а не пакет ───────────────────────────────
опубликовать_сборку || exit 1
