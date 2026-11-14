#!/usr/bin/env bash
#
# publish.sh — перенести ПРОВЕРЕННОЕ в публичную ветку харнеса.
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
# Имена веток — ДАННЫЕ (harness.conf): PUBLIC_BRANCH и PUBLIC_REMOTE. Своих
# умолчаний нет — схему выбирает владелец, а не код.
#
# Запуск:
#   bash scripts/publish.sh            # проверить и опубликовать
#   bash scripts/publish.sh --проверка # только проверить, ничего не менять
#   bash scripts/publish.sh --selftest
#   bash scripts/publish.sh --переписать-историю <каталог-копии> --бэкап <файл.bundle>
#                                       # перепись публичной истории (см. ниже)
#
# Код возврата: 0 — опубликовано (или проверка прошла), 1 — отказ замка,
# 2 — не настроено.
set -uo pipefail

# shellcheck disable=SC1091
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
# PUBLIC_PASPORT — путь паспорта для сборщика: только от паспорта двери (ниже), не из окружения;
# самотест называет свой двор присваиванием уже ПОСЛЕ загрузки.
konf_zagruzit_dveri STARTER_DIR PUBLIC_BRANCH PUBLIC_REMOTE PUBLIC_BUILD_DIR PUBLIC_COMMIT_EMAIL PUBLIC_PASPORT PAKET_SHA PREDMET_ZAMKA

# Судья слова владельца — рядом с этим скриптом, а не по окружению: путь от реального файла.
SCRIPTS_REAL="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PROJECT_DIR="${PROJECT_DIR:?пуст PROJECT_DIR — не прочитан паспорт установки}"
# Имя агента — из паспорта: вшитое сюда, оно след НАШЕЙ установки.
PACKAGE_DIR="${STARTER_DIR:-/home/$(konf_iz_fajla AGENT_USER)/starter/STARTER-PACKAGE}"

say() { printf '%s\n' "$*"; }

# ── Манифест слоя 1: что в выпуске личность и стандарты ────────────────────
# Эталон личности печатает root-скрипт self-seal по ЭТОМУ манифесту, поэтому
# манифест пишется тут, на выпуске, а не в момент печати. Слой 2 (instance.yaml)
# в манифест не входит: он у каждой установки свой. Прошлый манифест читается
# ДО записи нового — он может лежать там же, куда пишем.
# Строка, различная в старом и новом, — файл изменён, добавлен или удалён.
# $1=прошлый манифест (текст), $2=новый (текст).
сказать_смену_слоя1() {
    local changed
    changed="$(printf '%s\n%s\n' "$1" "$2" | grep . | LC_ALL=C sort | uniq -u \
        | sed 's/^[0-9a-f]\{64\}  //' | LC_ALL=C sort -u | paste -sd, | sed 's/,/, /g')"
    [ -z "$changed" ] || say "меняются личность/стандарты: $changed"
}

# Дверь для запроса согласия (ask-consent.py): считает и пишет манифест в пакет.
# Выпуск его не использует — манифест публичной сборки пишет build-public.py.
# $1=пакет, $2=прошлый манифест (нет файла — прошлого нет, названы все файлы).
manifest_sloya1() {
    local self="$1/harness/self" prev="$2" old="" new
    [ -d "$self" ] || { say "ОТКАЗ: у пакета нет каталога $self"; return 1; }
    [ -f "$prev" ] && old="$(LC_ALL=C sort "$prev")"
    new="$(cd "$self" && find . -type f ! -path ./MANIFEST.sha256 ! -path ./instance.yaml -printf '%P\n' \
        | LC_ALL=C sort | xargs -d '\n' -r sha256sum)"
    [ -n "$new" ] || { say "ОТКАЗ: в $self нет файлов слоя 1"; return 1; }
    printf '%s\n' "$new" > "$self/.MANIFEST.sha256.new" && mv -f "$self/.MANIFEST.sha256.new" "$self/MANIFEST.sha256" \
        || { say "ОТКАЗ: манифест слоя 1 не записан в $self"; return 1; }
    сказать_смену_слоя1 "$old" "$new"
    return 0
}

# Дверь стоит ДО проверки веток: манифест не про публичную ветку, и запрос
# согласия зовёт её на каждую публикацию.
if [ "${1:-}" = "--манифест-слоя1" ]; then
    [ "$#" = 3 ] || { say "ОТКАЗ: нужно --манифест-слоя1 <пакет> <прошлый манифест>"; exit 1; }
    manifest_sloya1 "$2" "$3" || exit 1
    exit 0
fi

# ── Дверь замка 1: слово владельца на пропуск полного прогона ──────────────
# Замок «ворота зелёные» поставил владелец и вправе снять его на выпуск; снятие
# обязано быть законной дверью, а не обходом ([[a-guard-without-a-legal-door]]).
# Дверь — файл $LOG_DIR/publish-gates-waiver.json: дословная цитата, id входящего,
# причина, срок. Срок конечный (не больше 6 часов с выдачи): запись без срока или
# просроченная — не дверь. Окружение дверью быть не может. Вместо полного прогона
# идёт быстрое подмножество, и пропуск пишется в журнал публикации.
# Коды: 0 — дверь открыта (цитата в stdout), 3 — записи нет, 1 — запись негодна.
PRAVO_NA_PROPUSK_MAX=21600
proverit_zapis_propuska() { # $1=каталог журнала
    python3 -I - "$1/publish-gates-waiver.json" "$PRAVO_NA_PROPUSK_MAX" <<'PY'
import json, sys, time
from pathlib import Path
путь, потолок = Path(sys.argv[1]), int(sys.argv[2])
if not путь.is_file():
    sys.exit(3)
try:
    з = json.loads(путь.read_text(encoding="utf-8"))
    поля = [str(з.get(k) or "").strip() for k in ("слово", "id_входящего", "причина")]
    выдана, до = int(з["выдана"]), int(з["до"])
except (OSError, ValueError, KeyError, TypeError, AttributeError) as беда:
    print(f"запись о пропуске ворот нечитаема: {беда}", file=sys.stderr); sys.exit(1)
if not all(поля):
    print("в записи о пропуске пусты слово, id входящего или причина", file=sys.stderr); sys.exit(1)
if not 0 < до - выдана <= потолок:
    print(f"срок записи о пропуске вне 0..{потолок} с", file=sys.stderr); sys.exit(1)
if до < time.time():
    print("запись о пропуске ворот просрочена", file=sys.stderr); sys.exit(1)
print(поля[0])
PY
}
# Та же запись снимает и замок 3 (статусы задач): слово «без прогона полного»
# владелец сказал про выпуск целиком. Честность держится оглаской: журнал
# публикации и список непроверенных в канал владельцу, статус done не ставится.
# $1=каталог журнала, $2=список непроверенных; 0 — пропуск по записи, иначе 1.
propustit_statusy_po_slovu() {
    local katalog="$1" spisok="$2" slovo
    slovo=$(proverit_zapis_propuska "$katalog" 2>/dev/null) || return 1
    printf '%s статусы пропущены по слову владельца «%s»; непроверенных: %s\n' \
        "$(date -Is)" "$slovo" "$(printf '%s\n' "$spisok" | grep -c .)" >> "$katalog/publish.log"
    say "замок 3 · статусы пропущены по слову владельца «$slovo»; уезжает непроверенное — $(printf '%s\n' "$spisok" | grep -c .) задач(и):"
    printf '%s\n' "$spisok" | sed 's/^/  • /'
    [ "$CHECK_ONLY" = 1 ] && return 0
    bash "$SCRIPTS_REAL/tg_send.sh" "Публикую по твоему слову «$slovo». В публичную ветку уезжает непроверенное тобой: $(printf '%s\n' "$spisok" | sed 's/ (статус.*//' | paste -sd, | sed 's/,/, /g')" >/dev/null 2>&1 || true
}

zamok_vorot() { # $1=каталог журнала [$2=каталог скриптов ворот] → 0 пустить, 1 отказ
    local katalog="$1" skripty="${2:-$SCRIPTS_REAL}" slovo rc
    slovo=$(proverit_zapis_propuska "$katalog" 2>/tmp/публикация-пропуск.err); rc=$?
    if [ "$rc" = 3 ]; then
        bash "$skripty/gates.sh" > /tmp/публикация-ворота.log 2>&1 || {
            say "ОТКАЗ: ворота красные — публиковать нельзя. Хвост:"
            tail -5 /tmp/публикация-ворота.log; return 1; }
        say "замок 1 · ворота зелёные"; return 0
    fi
    if [ "$rc" != 0 ]; then
        say "ОТКАЗ: $(cat /tmp/публикация-пропуск.err)"; return 1
    fi
    if ! { bash "$skripty/gates.sh" --хакатон \
           && bash "$skripty/check-secrets.sh" "$PROJECT_DIR" \
           && python3 -I "$skripty/build-public.py" --selftest; } \
           > /tmp/публикация-ворота.log 2>&1; then
        say "ОТКАЗ: быстрые ворота красные — публиковать нельзя. Хвост:"
        tail -5 /tmp/публикация-ворота.log; return 1
    fi
    printf '%s ворота пропущены по слову владельца «%s»; быстрое подмножество зелёное\n' \
        "$(date -Is)" "$slovo" >> "$katalog/publish.log"
    say "замок 1 · ворота пропущены по слову владельца «$slovo»; быстрое подмножество зелёное"
}

# ── Задачи, попадающие на витрину, обязаны быть ПРОВЕРЕНЫ владельцем ────────
# Трейлер «Dev-Map: <id>» стоит в коммитах ПРОЕКТА, а публикуется ПАКЕТ: они
# коммитятся парой, и связать их можно только временем. Поэтому спрашиваем:
# какие задачи появились в проекте ПОСЛЕ момента, на котором стоит публичная
# ветка. Читать историю пакета бесполезно — трейлеров там ноль, и замок
# пропускал бы всё и всегда (замер 11.09.2026, поймано до включения).
#
# Чтение трейлеров — отдельной функцией, и вот почему. Самотест судил её через
# ЖИВУЮ историю этой машины («есть ли за сутки хоть один трейлер») и 21.09.2026
# покраснел на исправном коде: с 14.09 коммитов просто не было. Проба обязана
# СОЗДАВАТЬ своё условие, а не совпадать с состоянием машины
# ([[a-check-creates-its-own-condition]]) — с отдельной функцией она строит свой
# репозиторий со своим трейлером и судит то же, что зовёт бой.
#
# Отказ git и пустая история — РАЗНЫЕ ответы (код 2 против пустого вывода):
# «не прочитали» ≠ «нечего проверять», и замок, прочитавший отказ как чистоту,
# выпустил бы наружу непроверенное ([[command-failure-read-as-empty]]).
ids_iz_istorii() { # $1=момент ISO (пусто — вся история) → id задач из трейлеров
    local since="$1" vyvod
    local ogr=()
    [ -n "$since" ] && ogr=(--since="$since")
    vyvod=$(git -C "$PROJECT_DIR" log "${ogr[@]}" --format=%B 2>/dev/null) || return 2
    printf '%s' "$vyvod" | sed -n 's/^Dev-Map: *//p' | sort -u
}

непроверенные_с() { # $1=момент ISO → печатает id задач, не готовых к витрине
    local ids rc
    ids=$(ids_iz_istorii "$1"); rc=$?
    if [ "$rc" = 2 ]; then
        printf 'ОТКАЗ-ИСТОРИИ\n'   # читатель обязан остановиться, а не считать это чистотой
        return 0
    fi
    [ -n "$ids" ] || return 0
    # Разбор — отдельным файлом: его зовут двое, замок в бою и проба в
    # самотесте. Копия внутри оболочки досталась бы только замку.
    DEVMAP="$PROJECT_DIR/dev-map.yaml" \
    DEVMAP_ARCHIVE="$PROJECT_DIR/docs/dev-map-archive.yaml" \
    IDS="$ids" python3 -I "$SCRIPTS_REAL/vitrina_status.py"
}

# ── Замки 4-6 и публикация собранного дерева ────────────────────────────────
# Наружу уезжает не пакет, а СБОРКА: инструмент без нашей мастерской. Порядок
# замков — единственное, что здесь своё; сборка, поиск секретов и поиск следов
# зовутся готовыми.
опубликовать_сборку() {  # [--сухо] — прогнать замки 4-6 и выйти, ничего не отправляя
    local sborka="$PUBLIC_BUILD_DIR" paket="$PACKAGE_DIR" itog hesh suho=0
    [ "${1:-}" = "--сухо" ] && suho=1
    # Собирается ровно тот коммит пакета, на который дано согласие (замок 2): HEAD,
    # прочитанный здесь заново, мог уйти вперёд за время замка 1.
    local paket_sha="${PAKET_SHA:-$(git -C "$paket" rev-parse HEAD 2>/dev/null)}"
    # Прошлый выпуск — манифест в каталоге сборки: снять ДО сборки, она его заменит.
    local proshlyj=""
    [ -f "$sborka/harness/self/MANIFEST.sha256" ] && proshlyj="$(LC_ALL=C sort "$sborka/harness/self/MANIFEST.sha256")"

    # Замок 4: собрать из УЧТЁННОГО состояния пакета.
    # Паспорт передаётся ЯВНО: без довода сборщик и гейт читают боевой
    # /etc/harness/install.conf, и самотест судил машину, а не код — «здоровая»
    # проба зеленела лишь потому, что настоящие имена не встретились в
    # подставном дереве (ревью кода 12.09.2026, №10).
    local pasport="${PUBLIC_PASPORT:-$KONF_DVERI_KOREN/install.conf}"
    itog=$(python3 -I "$SCRIPTS_REAL/build-public.py" \
                   --пакет "$paket" --куда "$sborka" --паспорт "$pasport" \
                   --коммит "$paket_sha" 2>&1) || {
        say "ОТКАЗ: сборка не прошла — $itog"; return 1; }
    hesh=$(printf '%s' "$itog" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["пакет"])' 2>/dev/null)
    # Пустой хэш означает, что вывод сборщика не разобран: дальше он ушёл бы в
    # сообщение коммита строкой «пакет » — и публичная история перестала бы
    # отвечать, из какого состояния пакета собрана (ревью кода 12.09.2026, №12).
    [ -n "$hesh" ] || { say "ОТКАЗ: не разобран вывод сборщика, хэш пакета пуст"; return 1; }
    say "замок 4 · собрано: $itog"
    # Новый манифест написал сборщик по собранному дереву.
    сказать_смену_слоя1 "$proshlyj" "$(LC_ALL=C sort "$sborka/harness/self/MANIFEST.sha256" 2>/dev/null)"

    # Своя история у сборки: публичная ветка ведётся здесь, а не в пакете.
    [ -d "$sborka/.git" ] || git -C "$sborka" init -q -b "$PUBLIC_BRANCH"
    # Удалённый настраивается КАЖДЫЙ раз, а не только при создании .git:
    # каталог сборки мог появиться раньше (проверка секретов, ручной осмотр),
    # и тогда публичной ветки в нём нет — замок 3 судил бы по пустоте.
    # Адрес берётся у ПАКЕТА и выверяется КАЖДЫЙ раз. Прежняя редакция
    # настраивала его лишь при отсутствии: сменив адрес публичного репозитория,
    # владелец продолжал бы отправлять по старому — молча. А пустая подстановка
    # (`remote add jig ""`) завершается успешно и навсегда закрывает починку:
    # `get-url` после неё отвечает 0 (ревью кода 12.09.2026, №6).
    local adres tekushchij
    adres=$(git -C "$paket" remote get-url "$PUBLIC_REMOTE" 2>/dev/null || true)
    [ -n "$adres" ] || { say "ОТКАЗ: у пакета нет удалённого «$PUBLIC_REMOTE» — публиковать некуда"; return 1; }
    tekushchij=$(git -C "$sborka" remote get-url "$PUBLIC_REMOTE" 2>/dev/null || true)
    if [ -z "$tekushchij" ]; then
        git -C "$sborka" remote add "$PUBLIC_REMOTE" "$adres"
    elif [ "$tekushchij" != "$adres" ]; then
        say "адрес публичного изменился: $tekushchij → $adres"
        git -C "$sborka" remote set-url "$PUBLIC_REMOTE" "$adres"
    fi
    git -C "$sborka" fetch --quiet "$PUBLIC_REMOTE" 2>/dev/null || true
    git -C "$sborka" add -A

    # Замок 5 (И-3): у нового репозитория своих хуков нет, и сторож секретов
    # его не сторожит — зовём явно, по временному индексу.
    local vyvod
    if ! vyvod=$(bash "$SCRIPTS_REAL/check-secrets.sh" "$sborka" 2>&1); then
        say "ОТКАЗ: в сборке похожее на секрет — публикация отменена"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
    say "замок 5 · секретов в сборке нет"

    # Замок 6: следы установки. Образцы берутся не из данных сборщика.
    if ! vyvod=$(python3 -I "$SCRIPTS_REAL/check-public-clean.py" "$sborka" \
                         --пакет "$paket" --проект "$PROJECT_DIR" \
                         --паспорт "$pasport" 2>&1); then
        say "ОТКАЗ: в сборке следы этой установки — публикация отменена"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
    say "замок 6 · следов установки в сборке нет"

    # Замок 6в: релиз уходит с записью простым языком (слово владельца 24.10.2026:
    # «в релизы харнеса писать человеческим языком что было исправлено добавлено»).
    # Судится только когда есть что выпускать: пустой индекс — это повтор упавшей
    # отправки или «публиковать нечего», и запись тогда уже вышла по праву.
    if ! git -C "$sborka" diff --cached --quiet; then
        if ! vyvod=$(python3 -I "$SCRIPTS_REAL/check-release-notes.py" "$sborka" 2>&1); then
            say "ОТКАЗ: релиз без записи простым языком — публикация отменена"
            printf '%s\n' "$vyvod" | sed 's/^/  /'
            return 1
        fi
        say "замок 6в · заметки выпуска простым языком есть"
    fi

    # Сухой прогон доходит ровно досюда: замки 4-6 ПРОГНАНЫ, а ничего не
    # закоммичено и не отправлено. Прежде «--проверка» печатала «публиковать
    # можно», не выполнив ни одного из них: владелец видел зелёное, тратил
    # согласие (оно протухает за час) — и настоящий прогон падал на замке 6.
    if [ "$suho" = 1 ]; then
        say "сухой прогон: замки 4, 5 и 6 пройдены — публиковать можно"
        return 0
    fi

    # Состояние публичной ветки спрашивается у САМОГО удалённого (ls-remote),
    # а не у локальной копии ссылки: после упавшего push дерево не менялось,
    # индекс тих, а remote-tracking помнит прежнее — и прежняя редакция
    # отвечала «публиковать нечего» с кодом 0, хотя коммит так и не уехал
    # (ревью кода 12.09.2026, И-4: это ложь о состоянии публичной ветки).
    local tam
    tam=$(git -C "$sborka" ls-remote "$PUBLIC_REMOTE" \
          "refs/heads/$PUBLIC_BRANCH" 2>/dev/null | cut -f1)
    if git -C "$sborka" diff --cached --quiet \
       && [ -n "$tam" ] \
       && [ "$(git -C "$sborka" rev-parse HEAD 2>/dev/null)" = "$tam" ]; then
        say "публиковать нечего: опубликованное совпадает со сборкой"
        return 0
    fi
    # Нечего коммитить, но HEAD впереди удалённой — значит прошлый push не
    # прошёл: коммит не делаем, а отправку повторяем.
    if git -C "$sborka" diff --cached --quiet; then
        say "новых изменений нет, но прошлая отправка не прошла — повторяю отправку"
    else
        # Номер версии, файл VERSION и секция CHANGELOG считаются и пишутся ДО
        # коммита: они едут ТЕМ ЖЕ коммитом, что и сборка. Разъедься они —
        # человек прочитал бы заметки одной версии рядом с кодом другой.
        # Первая строка сообщения НЕ меняется: её разбирает update.py
        # (`бытие_из_своего_пакета`), и новый формат погасил бы извещение об
        # обновлении у всех, кто уже поставил харнес. Версия едет телом и тегом.
        local otvet_versii nomer_versii
        # Имя удалённого передаётся ЯВНО: прошлая версия считается по тегам,
        # которые реально опубликованы. По локальным тегам счёт после упавшей
        # отправки тега пропускал бы номер, оставляя вышедший релиз без метки
        # навсегда (ревью кода 12.09.2026, R-2).
        otvet_versii=$(python3 -I "$SCRIPTS_REAL/version.py" --оформить \
                       --пакет "$paket" --сборка "$sborka" \
                       --проект "$PROJECT_DIR" \
                       --удалённый "$PUBLIC_REMOTE" 2>&1) || {
            say "ОТКАЗ: версия релиза не посчитана — $otvet_versii"; return 1; }
        nomer_versii=$(printf '%s' "$otvet_versii" \
            | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["следующая"]); sys.stderr.write(d["почему"])' 2>/dev/null)
        [ -n "$nomer_versii" ] || {
            say "ОТКАЗ: ответ version.py не разобран: ${otvet_versii:0:120}"; return 1; }
        say "версия релиза: v$nomer_versii"
        # Заметки релиза собраны машиной из ЗАГОЛОВКОВ КОММИТОВ ПАКЕТА, а их
        # пишет смена — и назвать продукт в заголовке может так же легко, как в
        # коде. CHANGELOG появляется ПОСЛЕ замка 6, поэтому судья чистоты
        # зовётся второй раз: уже по дереву с заметками (замер 13.10.2026 — в
        # заметки попадало 40 заголовков из 532).
        if ! vyvod=$(python3 -I "$SCRIPTS_REAL/check-public-clean.py" "$sborka" \
                             --пакет "$paket" --проект "$PROJECT_DIR" \
                             --паспорт "$pasport" 2>&1); then
            say "ОТКАЗ: следы установки в заметках релиза — публикация отменена"
            printf '%s\n' "$vyvod" | sed 's/^/  /'
            return 1
        fi
        say "замок 6б · в заметках релиза следов установки нет"
        # Индекс собран ДО замков — VERSION и CHANGELOG появились после него.
        git -C "$sborka" add -A
        git -C "$sborka" -c user.name="$PUBLIC_COMMIT_NAME" \
            -c user.email="$PUBLIC_COMMIT_EMAIL" \
            commit -q -m "версия от $(date +%F), пакет $hesh" \
                      -m "версия v$nomer_versii" || {
            say "ОТКАЗ: коммит сборки не прошёл"; return 1; }
    fi

    # Между замком 2 и пушем прошли минуты (замки 1, 3-6, версия): пакет, адрес,
    # вершина публичной ветки и коммиты сборки обязаны быть теми, что одобрил владелец.
    if [ "$(git -C "$paket" rev-parse HEAD 2>/dev/null)" != "$paket_sha" ]; then
        say "ОТКАЗ: пакет сдвинулся после согласия — не отправлено"; return 1
    fi
    if [ -n "${PREDMET_ZAMKA:-}" ] && ! vyvod=$(python3 -I "$SCRIPTS_REAL/lib/door_subjects.py" \
            сверить-публикацию "$PREDMET_ZAMKA" "$paket" "$sborka" "$PUBLIC_REMOTE" "$PUBLIC_BRANCH" 2>&1); then
        say "ОТКАЗ: предмет публикации изменился после согласия — не отправлено: $vyvod"; return 1
    fi

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
    # Тег ставится ПОСЛЕ успешной отправки ветки, и только так: тег на
    # неотправленный коммит — обещание версии, которой нет ни у кого.
    # Номер берётся из VERSION уже ОПУБЛИКОВАННОГО дерева, а не из ответа
    # счётчика: на пути «коммитить нечего, повторяю отправку» счётчик не
    # звался вовсе, а тег прошлой попытки всё равно надо дожать.
    local nomer teg
    nomer=$(tr -d '[:space:]' < "$sborka/VERSION" 2>/dev/null || true)
    if [ -z "$nomer" ]; then
        say "ВНИМАНИЕ: в сборке нет VERSION — релиз опубликован без тега"
    else
        teg="v$nomer"
        if git -C "$sborka" rev-parse -q --verify "refs/tags/$teg" >/dev/null; then
            say "тег $teg уже стоит"
        elif ! git -C "$sborka" -c user.name="$PUBLIC_COMMIT_NAME" \
                  -c user.email="$PUBLIC_COMMIT_EMAIL" \
                  tag -a "$teg" -m "релиз $teg, пакет $hesh"; then
            say "ОТКАЗ: тег $teg не поставлен — ветка опубликована, метки версии нет"
            return 1
        fi
        if ! git -C "$sborka" push --quiet "$PUBLIC_REMOTE" "refs/tags/$teg" 2>&1; then
            say "ОТКАЗ: тег $teg не отправлен — ветка опубликована, у людей нет метки версии"
            return 1
        fi
        say "релиз помечен: $teg"
    fi
    say "опубликовано: $PUBLIC_BRANCH ← сборка из пакета $hesh"
    return 0
}

# ── Дверь переписи публичной истории ───────────────────────────────────────
# Обычный путь — только вперёд (ff-only), и это держит обновление у тех, кто
# уже склонировал. Перепись истории — решение владельца, и ей нужна законная
# дверь, а не ручной force: сторож команд пускает к публичному удалённому
# только этот скрипт. Дверь отправляет ГОТОВУЮ копию истории одной атомарной
# отправкой, и каждая ссылка отправляется с арендой на тот sha, что дверь
# прочла: чужой коммит, пришедший между чтением и отправкой, отвергнет всё.
# Срок кнопки на перепись — owner_word.СРОК_ВИДА_МИН (60 мин).

# Ссылки репозитория, ветки и теги: «sha ref», отсортированно. Теги — sha
# самого объекта тега, не пересечённый: аренда сверяется именно с ним.
ssylki_kopii() { # $1=репозиторий
    git -C "$1" for-each-ref --format='%(objectname) %(refname)' refs/heads refs/tags | sort
}

# Ссылки публичного удалённого по адресу; HEAD и «^{}» отбрасываются.
ssylki_udalyonnogo() { # $1=адрес → «sha ref»; код 1, если спросить не удалось
    local otvet
    otvet=$(git ls-remote "$1" 'refs/heads/*' 'refs/tags/*' 2>/dev/null) || return 1
    printf '%s\n' "$otvet" | awk 'NF == 2 && $2 !~ /\^\{\}$/ {print $1, $2}' | sort
}

# Бэкап годен, если verify проходит и из него реально разворачивается
# репозиторий: verify судит заголовок и обрезанный пакет не замечает.
proverit_bekap() { # $1=файл $2=рабочий каталог
    [ -f "$1" ] || { say "ОТКАЗ: бэкап (bundle) не найден: $1"; return 1; }
    git init -q "$2/verify" && git -C "$2/verify" bundle verify "$1" >/dev/null 2>&1 \
        || { say "ОТКАЗ: бэкап (bundle) не проходит git bundle verify: $1"; return 1; }
    git clone -q --bare "$1" "$2/clone" >/dev/null 2>&1 && git -C "$2/clone" fsck --no-dangling >/dev/null 2>&1 \
        || { say "ОТКАЗ: бэкап (bundle) не разворачивается в репозиторий — пакет внутри повреждён: $1"; return 1; }
}

# Прежняя публичная история обязана лежать в бэкапе целиком: откат из него —
# единственный путь назад.
bekap_pokryvaet() { # $1=файл $2=«sha ref» удалённого
    local golovy stroka
    golovy=$(git bundle list-heads "$1" 2>/dev/null)
    while IFS= read -r stroka; do
        grep -qxF "$stroka" <<< "$golovy" || {
            say "ОТКАЗ: в бэкапе (bundle) нет ссылки «${stroka#* }» с её нынешним sha — бэкап устарел"
            return 1; }
    done <<< "$2"
}

# Ссылки удалённого и копии должны совпадать ПО ИМЕНАМ: лишнее у удалённого
# (чужая работа) или у копии (тег, которого люди не видели) — решение человека.
sverit_imena_ssylok() { # $1=«sha ref» удалённого $2=«sha ref» копии
    local tam u_nas lishnee
    tam=$(printf '%s\n' "$1" | cut -d' ' -f2 | LC_ALL=C sort); u_nas=$(printf '%s\n' "$2" | cut -d' ' -f2 | LC_ALL=C sort)
    lishnee=$(LC_ALL=C comm -23 <(printf '%s\n' "$tam") <(printf '%s\n' "$u_nas") | paste -sd' ')
    [ -z "$lishnee" ] || { say "ОТКАЗ: на удалённом есть ссылки, которых нет в копии: $lishnee"; return 1; }
    lishnee=$(LC_ALL=C comm -13 <(printf '%s\n' "$tam") <(printf '%s\n' "$u_nas") | paste -sd' ')
    [ -z "$lishnee" ] || { say "ОТКАЗ: в копии есть ссылки, которых нет на удалённом: $lishnee"; return 1; }
}

# Гейт чистоты по ИСТОРИИ копии. Сам гейт на историю отвечает кодом 0 и лишь
# предупреждает (убрать её можно только перезаписью — то есть этой дверью), а
# здесь перепись и есть то, что должно убрать следы: дверь считает находки сама.
# Свежесть метки не судим: дерево копии уже опубликовано и сверено с деревом
# сборки, а метка называет пакет, из которого оно собрано; пересобрать нельзя.
proverit_chistotu_kopii() { # $1=копия $2=паспорт
    local vyvod rc=0
    vyvod=$(python3 -I "$SCRIPTS_REAL/check-public-clean.py" "$1" \
                --пакет "$PACKAGE_DIR" --проект "$PROJECT_DIR" --паспорт "$2" \
                --свежесть-не-судить 2>&1) || rc=$?
    if [ "$rc" != 0 ] || grep -q '⚠ история' <<< "$vyvod"; then
        say "ОТКАЗ: в копии остались следы установки (гейт чистоты, код $rc)"
        printf '%s\n' "$vyvod" | sed 's/^/  /'
        return 1
    fi
}

# Согласие владельца на перепись — «Да» кнопкой на предмет переписи (вершина удалённого ДО входит
# в предмет: после переписи прежнее «Да» повторный force-push не откроет). Нет кнопки — ворота
# сами просят её; рабочий каталог двери убирается на выходе.
RABOCHIJ_KATALOG_DVERI=""
zavershit_dver() {
    [ -z "$RABOCHIJ_KATALOG_DVERI" ] || rm -rf "$RABOCHIJ_KATALOG_DVERI"
}

PREDMET_PEREPISI=""
vzyat_soglasie_dveri() { # $1=копия
    local otvet
    otvet=$(HARNESS_LOG_DIR="$LOG_DIR" python3 -I "$SCRIPTS_REAL/deploy_guard.py" --действие перепись \
                --только-согласие --просить --перепись "$1" 2>&1) || {
        say "ОТКАЗ: $otvet"
        return 1; }
    # Предмет, по которому открыта дверь; перед force-push он пересчитывается.
    PREDMET_PEREPISI=$(printf '%s\n' "$otvet" | sed -n 's/^предмет: //p' | tail -1)
    [ -n "$PREDMET_PEREPISI" ] || { say "ОТКАЗ: ворота не назвали предмет переписи"; return 1; }
}

# Сборка переводится на новую историю: fetch берёт ветку и теги v*, ветка
# переставляется под проверкой старого sha, индекс сбрасывается.
perevesti_sborku() { # $1=адрес $2=новый sha ветки
    local sborka="$PUBLIC_BUILD_DIR" star
    star=$(git -C "$sborka" rev-parse "refs/heads/$PUBLIC_BRANCH") || return 1
    git -C "$sborka" remote set-url "$PUBLIC_REMOTE" "$1" 2>/dev/null \
        || git -C "$sborka" remote add "$PUBLIC_REMOTE" "$1" || return 1
    git -C "$sborka" fetch -q "$PUBLIC_REMOTE" \
            "+refs/heads/$PUBLIC_BRANCH:refs/remotes/$PUBLIC_REMOTE/$PUBLIC_BRANCH" '+refs/tags/v*:refs/tags/v*' \
        && git -C "$sborka" update-ref "refs/heads/$PUBLIC_BRANCH" "$2" "$star" \
        && git -C "$sborka" reset -q
}

perepisat_istoriyu() { # $1=каталог копии $2=файл бэкапа
    local kopiya bekap adres pasport tam u_nas refy="" stroka sha ref novyj soobshchenie
    local -a arendy=() spisok=()
    kopiya=$(readlink -f "$1" 2>/dev/null); bekap=$(readlink -f "$2" 2>/dev/null)
    say "=== перепись публичной истории: копия → $PUBLIC_REMOTE/$PUBLIC_BRANCH ==="
    git -C "${kopiya:-/нет}" rev-parse --git-dir >/dev/null 2>&1 \
        || { say "ОТКАЗ: копии (репозитория) нет: $1"; return 1; }
    git -C "$kopiya" rev-parse -q --verify "refs/heads/$PUBLIC_BRANCH" >/dev/null \
        || { say "ОТКАЗ: в копии нет ветки «$PUBLIC_BRANCH»"; return 1; }
    [ -n "$bekap" ] || { say "ОТКАЗ: не указан бэкап (bundle): --бэкап <файл.bundle>"; return 1; }
    RABOCHIJ_KATALOG_DVERI=$(mktemp -d)
    trap zavershit_dver EXIT
    proverit_bekap "$bekap" "$RABOCHIJ_KATALOG_DVERI" || return 1
    say "бэкап: $bekap, sha256 $(sha256sum "$bekap" | cut -d' ' -f1)"

    adres=$(git -C "$PACKAGE_DIR" remote get-url "$PUBLIC_REMOTE" 2>/dev/null || true)
    [ -n "$adres" ] || { say "ОТКАЗ: у пакета нет удалённого «$PUBLIC_REMOTE» — публиковать некуда"; return 1; }
    tam=$(ssylki_udalyonnogo "$adres") && [ -n "$tam" ] \
        || { say "ОТКАЗ: ссылок публичного удалённого прочитать не удалось (или их нет)"; return 1; }
    u_nas=$(ssylki_kopii "$kopiya")
    sverit_imena_ssylok "$tam" "$u_nas" || return 1
    bekap_pokryvaet "$bekap" "$tam" || return 1
    [ "$(git -C "$PUBLIC_BUILD_DIR" rev-parse "refs/heads/$PUBLIC_BRANCH" 2>/dev/null) refs/heads/$PUBLIC_BRANCH" \
        = "$(printf '%s\n' "$tam" | grep " refs/heads/$PUBLIC_BRANCH\$")" ] \
        || { say "ОТКАЗ: ветка сборки не равна ветке удалённого — в сборке неопубликованное"; return 1; }

    [ "$(git -C "$kopiya" rev-parse "refs/heads/$PUBLIC_BRANCH^{tree}")" \
        = "$(git -C "$PUBLIC_BUILD_DIR" rev-parse "refs/heads/$PUBLIC_BRANCH^{tree}" 2>/dev/null)" ] \
        || { say "ОТКАЗ: дерево ветки в копии не равно дереву локальной сборки — перепись меняет историю, не содержимое"; return 1; }
    # Паспорт передаётся явно, как в замке 6: иначе гейт читает боевой.
    pasport="${PUBLIC_PASPORT:-$KONF_DVERI_KOREN/install.conf}"
    proverit_chistotu_kopii "$kopiya" "$pasport" || return 1
    say "проверки пройдены: ссылки сверены, бэкап покрывает удалённый, дерево равно сборке, следов в истории нет"

    vzyat_soglasie_dveri "$kopiya" || return 1
    while read -r sha ref; do
        arendy+=("--force-with-lease=$ref:$sha")
    done <<< "$tam"
    # Без «+» в refspec: принудительность даёт ТОЛЬКО аренда. «+» отменил бы её проверку.
    while read -r sha ref; do
        spisok+=("$ref:$ref")
    done <<< "$u_nas"
    # Между «Да» и отправкой удалённый и копия обязаны остаться теми же, что на кнопке.
    soobshchenie=$(python3 -I "$SCRIPTS_REAL/lib/door_subjects.py" сверить-перепись "$PREDMET_PEREPISI" \
                       "$kopiya" "$PACKAGE_DIR" "$PUBLIC_REMOTE" "$PUBLIC_BRANCH" 2>&1) \
        || { say "ОТКАЗ: $soobshchenie"; return 1; }
    if ! git -C "$kopiya" push --atomic "${arendy[@]}" "$adres" "${spisok[@]}" 2>&1; then
        say "ОТКАЗ: атомарная отправка не прошла (удалённый ушёл вперёд или отверг) — ничего не изменено"
        return 1
    fi
    say "отправлено одной атомарной отправкой: ссылок $(printf '%s\n' "$u_nas" | grep -c .)"
    say "откат: бэкап $bekap; прежние ссылки удалённого:"
    printf '%s\n' "$tam" | sed 's/^/  /'
    say "откат: git clone --mirror $bekap <каталог> && git -C <каталог> push --mirror <адрес «$PUBLIC_REMOTE» из git remote get-url>"
    [ "$(ssylki_udalyonnogo "$adres")" = "$u_nas" ] \
        || { say "ОТКАЗ: после отправки ссылки удалённого не равны ссылкам копии — смотри откат выше"; return 1; }
    novyj=$(git -C "$kopiya" rev-parse "refs/heads/$PUBLIC_BRANCH")
    perevesti_sborku "$adres" "$novyj" \
        || { say "ОТКАЗ: удалённый переписан, но сборку перевести не удалось — смотри откат выше"; return 1; }
    say "переписано: удалённый == копия, сборка переведена на новую историю"
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
    # Дверь берёт паспорт рядом с СОБОЙ, а не из окружения: стенд кладёт КОПИЮ двери в
    # свой каталог, и паспорт стенда ей даёт HARNESS_DOOR_STAND_HOME (как у drop-database.sh).
    STEND=$(mktemp -d); mkdir -p "$STEND/scripts"
    cp "$SCRIPTS_REAL/publish.sh" "$STEND/scripts/publish.sh"
    ln -s "$SCRIPTS_REAL/lib" "$STEND/scripts/lib"
    PUSTOJ_KONFIG="$STEND/install.conf"
    printf 'PROJECT_DIR="%s"\nPUBLIC_BRANCH=""\n' "$(mktemp -d)" > "$PUSTOJ_KONFIG"
    OUT=$(HARNESS_DOOR_STAND_HOME="$STEND" bash "$STEND/scripts/publish.sh" --проверка 2>&1); RC=$?
    if [ "$RC" = 2 ] && printf '%s' "$OUT" | grep -q "PUBLIC_BRANCH"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: ветки не заданы — отказ с внятной причиной"
    else
        echo "  ПЛОХО ветки не заданы: код $RC, ответ: ${OUT:0:120}"; ok=0
    fi
    # БОЛЬНОЙ СЛУЧАЙ: паспорта нет вовсе. Прежде здесь стоял наш путь
    # умолчанием — скрипт брался публиковать ЧУЖУЮ установку как свою.
    : > "$PUSTOJ_KONFIG"
    OUT=$(HARNESS_DOOR_STAND_HOME="$STEND" bash "$STEND/scripts/publish.sh" --проверка 2>&1); RC=$?
    rm -rf "$STEND"
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -q "PROJECT_DIR"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: без паспорта отказ вслух, а не чужой каталог"
    else
        echo "  ПЛОХО без паспорта: код $RC, ответ: ${OUT:0:120}"; ok=0
    fi
    # БОЛЬНОЙ СЛУЧАЙ 2: сегодняшние задачи лежат в статусе test — замок обязан
    # их назвать. Прежняя редакция читала историю ПАКЕТА, где трейлеров нет, и
    # молча отвечала «всё проверено».
    # Смысл пробы — ЧТЕНИЕ ИСТОРИИ: замок обязан брать id задач из истории
    # ПРОЕКТА (там трейлеры Dev-Map), а не пакета, где их ноль. Прежняя
    # редакция судила по тому, остались ли в живой карте непроверенные
    # задачи, — и 14.09.2026 покраснела ровно потому, что их закрыли: проба
    # совпадала с состоянием машины вместо того, чтобы создавать своё условие.
    # Проба СТРОИТ свою историю: репозиторий, два коммита, трейлер в одном.
    # Прежняя редакция считала трейлеры за сутки в ЖИВОЙ истории машины и
    # 21.09.2026 покраснела на исправном коде — с 14.09 коммитов не было.
    ISTORIYA_DVOR=$(mktemp -d)
    (
        cd "$ISTORIYA_DVOR" || exit 1
        git init -q . && git config user.email проба@проба && git config user.name проба
        echo первый > a.txt && git add a.txt
        git commit -q -m "без трейлера"
        echo второй > b.txt && git add b.txt
        printf 'с трейлером\n\nDev-Map: hr.проба-чтения-истории\n' | git commit -q -F -
    ) >/dev/null 2>&1
    NASHLI=$(PROJECT_DIR="$ISTORIYA_DVOR" ids_iz_istorii "")
    if [ "$NASHLI" = "hr.проба-чтения-истории" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: трейлер из истории ПРОЕКТА прочитан (своё условие, не живая машина)"
    else
        echo "  ПЛОХО замок не читает трейлер Dev-Map из истории: вышло «$NASHLI»"; ok=0
    fi
    # Коммит без трейлера в тот же счёт не идёт: иначе «прочитали» нельзя
    # отличить от «нашли что попало».
    if [ "$(PROJECT_DIR="$ISTORIYA_DVOR" ids_iz_istorii "" | grep -c .)" = 1 ]; then
        echo "  ок    коммит без трейлера в счёт не идёт"
    else
        echo "  ПЛОХО коммит без трейлера попал в счёт"; ok=0
    fi
    # ОТКАЗ git ≠ пустая история: замок, прочитавший отказ как чистоту, выпустил
    # бы наружу непроверенное.
    PROJECT_DIR="$ISTORIYA_DVOR/нет-такого" ids_iz_istorii "" >/dev/null 2>&1
    if [ $? = 2 ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: git отказал — код 2, а не пустота"
    else
        echo "  ПЛОХО отказ git прочитан как пустая история"; ok=0
    fi
    OTKAZ_VYVOD=$(PROJECT_DIR="$ISTORIYA_DVOR/нет-такого" непроверенные_с "")
    if [ "$OTKAZ_VYVOD" = "ОТКАЗ-ИСТОРИИ" ]; then
        echo "  ок    отказ доходит до читателя словом, а не молчанием"
    else
        echo "  ПЛОХО отказ истории не дошёл до читателя: «$OTKAZ_VYVOD»"; ok=0
    fi
    rm -rf "$ISTORIYA_DVOR"
    # БОЛЬНОЙ СЛУЧАЙ 3: разбор статусов витрины судится своим самотестом —
    # тем же файлом, который зовёт замок, а не его копией здесь.
    if python3 -I "$SCRIPTS_REAL/vitrina_status.py" --selftest | tail -1 \
       | grep -q "САМОТЕСТ ПРОЙДЕН"; then
        echo "  ок    разбор статусов витрины: «принято» не держит, «test» держит"
    else
        echo "  ПЛОХО самотест разбора статусов витрины не прошёл"; ok=0
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
        mkdir -p "$dvor/пакет/harness/config" "$dvor/пакет/harness/память" \
                 "$dvor/пакет/harness/шаблоны-задач" "$dvor/сборка"
        printf '# харнес\n@STATE.md\n' > "$dvor/пакет/harness/CLAUDE.md"
        printf 'состояние\n' > "$dvor/пакет/harness/STATE.md"
        printf '# ТОН\nзамер речи\n' > "$dvor/пакет/harness/память/TONE.md"
        printf '# ТОН\n\n<!-- потолки: мягкий=700 жёсткий=1500 -->\n' \
            > "$dvor/пакет/harness/шаблоны-задач/ТОН-заготовка.md"
        printf 'исключить: []\nзаменить_шаблоном:\n  harness/память/TONE.md: harness/шаблоны-задач/ТОН-заготовка.md\nобезличить_ключи: [PROJECT_DIR]\nоставить: [harness/CLAUDE.md]\n' \
            > "$dvor/пакет/harness/config/public-build.yaml"
        : > "$dvor/пакет/ЧТО-НОВОГО.md"
        proba_zapis "$dvor" "Первый выпуск"
        git -C "$dvor/пакет" init -q -b master
        git -C "$dvor/пакет" add -A
        git -C "$dvor/пакет" -c user.name=t -c user.email=t@t commit -qm проба
        git init -q --bare "$dvor/удалённый.git"
        git -C "$dvor/пакет" remote add jig "$dvor/удалённый.git"
        printf 'PROJECT_DIR="%s"\nAGENT_USER="подставной"\nLOG_DIR="%s"\nPROJECT_NAME="Подставной"\nTG_CHAT_ID="100000001"\nPRODUCT_DIR="tovar"\n' \
            "$PROJECT_DIR" "$dvor" > "$dvor/паспорт.conf"
        printf 'PUBLIC_REMOTE="jig"\nPUBLIC_BRANCH="master"\nPUBLIC_BUILD_DIR="%s"\nPUBLIC_COMMIT_NAME="проба-харнес"\nPUBLIC_COMMIT_EMAIL="proba@harness.local"\n' \
            "$dvor/сборка" > "$dvor/харнес.conf"
    }
    proba_zapis() {  # $1=двор $2=название: новая запись простым языком СВЕРХУ заметок
        local fajl="$1/пакет/ЧТО-НОВОГО.md" staroe
        staroe=$(cat "$fajl")
        printf '# Что нового\n\n## 2026-10-24 — %s\n\n### Исправлено\n\n- Сессия больше не обрывается.\n\n### Что изменилось для вас\n\n- Ничего делать не нужно.\n\n%s\n' \
            "$2" "${staroe#\# Что нового}" > "$fajl"
    }
    proba_publ() {  # запускает публикацию сборки в подставном дворе
        # ПОДОБОЛОЧКА: присваивания перед вызовом ФУНКЦИИ остаются в оболочке
        # после возврата, и дворы протекали друг в друга — проба, забывшая
        # задать переменную, зеленела на наследстве соседней.
        local dvor="$1"
        (
            PACKAGE_DIR="$dvor/пакет" PUBLIC_BUILD_DIR="$dvor/сборка" \
            PUBLIC_REMOTE=jig PUBLIC_BRANCH=master \
            PUBLIC_COMMIT_NAME="проба-харнес" PUBLIC_COMMIT_EMAIL="proba@harness.local" \
            PUBLIC_PASPORT="$dvor/паспорт.conf" \
            PROJECT_DIR="$PROJECT_DIR" опубликовать_сборку 2>&1
        )
    }
    kommitov() { git -C "$1" rev-list --count master 2>/dev/null || echo 0; }

    # ДВЕРЬ ЗАМКА 1: пропуск полного прогона по слову владельца. Двор — подставной
    # проект с заглушками ворот: проба судит выбор прогона, а не сами ворота.
    proba_vorota() { # $1=двор $2=запись (json или пусто) → печатает вывод замка
        local d="$1"
        mkdir -p "$d/проект/scripts" "$d/лог"
        printf '#!/bin/sh\necho "gates $*" >> "%s/звали"\n' "$d" > "$d/проект/scripts/gates.sh"
        printf '#!/bin/sh\necho "secrets" >> "%s/звали"\n' "$d" > "$d/проект/scripts/check-secrets.sh"
        printf 'open("%s/звали","a").write("build\\n")\n' "$d" > "$d/проект/scripts/build-public.py"
        [ -n "$2" ] && printf '%s' "$2" > "$d/лог/publish-gates-waiver.json"
        ( PROJECT_DIR="$d/проект" zamok_vorot "$d/лог" "$d/проект/scripts" 2>&1 ); echo "rc=$?"
    }
    zapis_propuska() { # $1=выдана-смещение $2=до-смещение (сек от сейчас)
        local n; n=$(date +%s)
        printf '{"слово":"без прогона полного","id_входящего":"1793351076.532892567","причина":"проба","выдана":%s,"до":%s}' \
            "$((n+$1))" "$((n+$2))"
    }
    OUT=$(proba_vorota "$T/в1" ""); if printf '%s' "$OUT" | grep -q 'rc=0' \
       && grep -qx 'gates ' "$T/в1/звали" && ! grep -q -- 'хакатон' "$T/в1/звали"; then
        echo "  ок    ворота без записи о пропуске — полный прогон, как раньше"
    else echo "  ПЛОХО без записи не полный прогон: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_vorota "$T/в2" "$(zapis_propuska -60 3600)"); if printf '%s' "$OUT" | grep -q 'rc=0' \
       && grep -q 'gates --хакатон' "$T/в2/звали" && grep -q secrets "$T/в2/звали" \
       && grep -q build "$T/в2/звали" && grep -q 'ворота пропущены по слову владельца «без прогона полного»' "$T/в2/лог/publish.log"; then
        echo "  ок    запись о пропуске — быстрое подмножество и строка в журнале публикации"
    else echo "  ПЛОХО запись о пропуске не открыла быструю дверь: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_vorota "$T/в3" "$(zapis_propuska -7200 -60)"); if printf '%s' "$OUT" | grep -q 'rc=1' \
       && printf '%s' "$OUT" | grep -q 'просрочена' && [ ! -e "$T/в3/звали" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: просроченная запись о пропуске — отказ, ворота не звались"
    else echo "  ПЛОХО просроченная запись: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_vorota "$T/в4" "$(zapis_propuska -60 999999)"); if printf '%s' "$OUT" | grep -q 'rc=1' \
       && printf '%s' "$OUT" | grep -q 'срок' && [ ! -e "$T/в4/звали" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: запись без конечного срока (дольше 6 часов) — не дверь"
    else echo "  ПЛОХО запись без срока пропущена: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_vorota "$T/в5" '{"слово":"","id_входящего":"1","причина":"x","выдана":1,"до":2}'); if printf '%s' "$OUT" | grep -q 'rc=1'; then
        echo "  ок    запись без цитаты владельца — не дверь"
    else echo "  ПЛОХО запись без цитаты пропущена: ${OUT:0:150}"; ok=0; fi

    # ДВЕРЬ ЗАМКА 3: та же запись пропускает статусы, владельцу уходит список.
    proba_statusy() { # $1=двор $2=запись → вывод и rc
        local d="$1"
        mkdir -p "$d/проект/scripts" "$d/лог"
        printf '#!/bin/sh\necho "$1" >> "%s/ушло"\n' "$d" > "$d/проект/scripts/tg_send.sh"
        [ -n "$2" ] && printf '%s' "$2" > "$d/лог/publish-gates-waiver.json"
        ( PROJECT_DIR="$d/проект" SCRIPTS_REAL="$d/проект/scripts" CHECK_ONLY=0 propustit_statusy_po_slovu "$d/лог" $'задача-а (статус test)\nзадача-б (статус test)' 2>&1 ); echo "rc=$?"
    }
    OUT=$(proba_statusy "$T/с1" ""); if printf '%s' "$OUT" | grep -q 'rc=1' && [ ! -e "$T/с1/ушло" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: статусы без записи о пропуске — замок 3 не снят"
    else echo "  ПЛОХО без записи статусы пропущены: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_statusy "$T/с2" "$(zapis_propuska -7200 -60)"); if printf '%s' "$OUT" | grep -q 'rc=1' && [ ! -e "$T/с2/ушло" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: статусы с просроченной записью — замок 3 не снят"
    else echo "  ПЛОХО просроченная запись сняла статусы: ${OUT:0:150}"; ok=0; fi
    OUT=$(proba_statusy "$T/с3" "$(zapis_propuska -60 3600)"); if printf '%s' "$OUT" | grep -q 'rc=0' \
       && grep -q 'задача-а, задача-б' "$T/с3/ушло" && grep -q 'статусы пропущены по слову владельца «без прогона полного»' "$T/с3/лог/publish.log"; then
        echo "  ок    запись о пропуске — статусы пропущены, список ушёл владельцу, строка в журнале"
    else echo "  ПЛОХО статусы по записи: ${OUT:0:150}"; ok=0; fi

    # СУХОЙ ПРОГОН: замки 4-6 обязаны выполниться, а коммита и отправки быть не
    # должно. Прежде «--проверка» печатала «публиковать можно», не выполнив ни
    # одного замка: владелец видел зелёное и тратил согласие впустую (ревью
    # кода 12.09.2026, №5).
    proba_dvor "$T/сухо"
    BYLO_SUHO=$(kommitov "$T/сухо/удалённый.git")
    OUT=$( (PACKAGE_DIR="$T/сухо/пакет" PUBLIC_BUILD_DIR="$T/сухо/сборка" \
            PUBLIC_REMOTE=jig PUBLIC_BRANCH=master \
            PUBLIC_COMMIT_NAME="проба-харнес" PUBLIC_COMMIT_EMAIL="proba@harness.local" \
            PUBLIC_PASPORT="$T/сухо/паспорт.conf" PROJECT_DIR="$PROJECT_DIR" \
            опубликовать_сборку --сухо 2>&1) ); RC=$?
    STALO_SUHO=$(kommitov "$T/сухо/удалённый.git")
    if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q 'замок 6' \
       && printf '%s' "$OUT" | grep -q 'сухой прогон' && [ "$BYLO_SUHO" = "$STALO_SUHO" ]; then
        echo "  ок    сухой прогон: замки 4-6 выполнены, ничего не отправлено"
    else
        echo "  ПЛОХО сухой прогон: код $RC, коммитов $BYLO_SUHO→$STALO_SUHO: $(printf '%s' "$OUT" | tail -2 | tr '\n' ' ')"; ok=0
    fi

    # БОЛЬНОЙ: след установки в коде сборки — отказ ДО push.
    proba_dvor "$T/чистота"
    printf 'ПУТЬ = "%s"\n' "$PROJECT_DIR" > "$T/чистота/пакет/harness/след.py"
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
        > "$T/секрет/пакет/harness/тайна.py"
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

    # Релиз по стандарту: у опубликованного есть НОМЕР, МЕТКА и ЗАМЕТКИ.
    # Проверяется удалённый, а не локальная сборка: тег, не уехавший наружу,
    # для человека не существует.
    TEGI=$(git -C "$T/здоровый/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    VERS=$(tr -d '[:space:]' < "$T/здоровый/сборка/VERSION" 2>/dev/null)
    ZHURNAL=$(cat "$T/здоровый/сборка/CHANGELOG.md" 2>/dev/null)
    if [ -n "$VERS" ] && printf '%s' "$TEGI" | grep -q "v$VERS" \
       && printf '%s' "$ZHURNAL" | grep -q "## \[$VERS\]" \
       && printf '%s' "$ZHURNAL" | grep -q "$HESH_PAKETA"; then
        echo "  ок    релиз: версия $VERS, тег на удалённом, журнал с хэшем пакета"
    else
        echo "  ПЛОХО релиз без метки: VERSION «$VERS», теги «$TEGI», журнала знаков ${#ZHURNAL}"; ok=0
    fi

    # ВТОРОЙ РЕЛИЗ тем же путём: номер растёт, журнал НАКАПЛИВАЕТ секции.
    # Условие создаётся боевым способом — новый файл-механизм в пакете, и весь
    # путь идёт целиком, вместе со сборщиком. Он зовёт `rsync -a --delete`, а
    # VERSION и CHANGELOG.md лежат в сборке, но не в пакете: без починки
    # 12.09.2026 журнал обнулялся на каждой публикации и история версий
    # исчезала, оставляя одну секцию.
    mkdir -p "$T/здоровый/пакет/harness/demons"
    printf '#!/bin/sh\necho второй демон присмотра\n' \
        > "$T/здоровый/пакет/harness/demons/новый.sh"
    proba_zapis "$T/здоровый" "Второй выпуск"
    git -C "$T/здоровый/пакет" add -A
    git -C "$T/здоровый/пакет" -c user.name=t -c user.email=t@t commit -qm "новый демон"
    OUT=$(proba_publ "$T/здоровый"); RC=$?
    VERS2=$(tr -d '[:space:]' < "$T/здоровый/сборка/VERSION" 2>/dev/null)
    TEGI2=$(git -C "$T/здоровый/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    SEKCIJ=$(grep -c '^## \[' "$T/здоровый/сборка/CHANGELOG.md" 2>/dev/null || echo 0)
    if [ "$RC" = 0 ] && [ "$VERS2" = "0.2.0" ] && [ "$SEKCIJ" = "2" ] \
       && printf '%s' "$TEGI2" | grep -q "v0.1.0" \
       && printf '%s' "$TEGI2" | grep -q "v0.2.0"; then
        echo "  ок    второй релиз: новый механизм дал минор $VERS2, тегов 2, секций в журнале 2"
    else
        echo "  ПЛОХО второй релиз: код $RC, версия «$VERS2», теги «$TEGI2», секций $SEKCIJ"; ok=0
    fi
    TEGI="$TEGI2"

    # БОЛЬНОЙ: удалённый ушёл вперёд чужим коммитом — отказ без force.
    CHUZHOJ=$(mktemp -d)
    git clone -q "$T/здоровый/удалённый.git" "$CHUZHOJ/копия"
    printf 'чужое\n' > "$CHUZHOJ/копия/чужое.txt"
    git -C "$CHUZHOJ/копия" add -A
    git -C "$CHUZHOJ/копия" -c user.name=ч -c user.email=ч@ч commit -qm чужое
    git -C "$CHUZHOJ/копия" push -q origin master
    BYLO=$(kommitov "$T/здоровый/удалённый.git")
    printf 'ещё\n' > "$T/здоровый/пакет/новое.txt"
    proba_zapis "$T/здоровый" "Третий выпуск"
    git -C "$T/здоровый/пакет" add -A
    git -C "$T/здоровый/пакет" -c user.name=t -c user.email=t@t commit -qm ещё
    OUT=$(proba_publ "$T/здоровый"); RC=$?
    # Тег — обещание версии. Ветка не уехала — тега быть не должно: иначе на
    # удалённом появилась бы метка на коммит, которого там нет.
    TEGI_POSLE=$(git -C "$T/здоровый/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    if [ "$RC" != 0 ] && printf '%s' "$OUT" | grep -qi 'расхожд' \
       && [ "$(kommitov "$T/здоровый/удалённый.git")" = "$BYLO" ] \
       && [ "$TEGI_POSLE" = "$TEGI" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: расхождение веток — отказ, история не переписана, тега нет"
    else
        echo "  ПЛОХО расхождение не остановило push: код $RC, ${OUT:0:100}"; ok=0
    fi
    rm -rf "$T" "$CHUZHOJ"

    # БОЛЬНОЙ: прошлая отправка не прошла (сеть, права, ff), дерево с тех пор
    # не менялось. Повтор обязан ОТПРАВИТЬ, а не сказать «публиковать нечего»:
    # прежняя редакция сверяла индекс с HEAD сборки и возвращала успех, хотя
    # коммит так и не уезжал (ревью кода 12.09.2026, И-4).
    # Условие создаётся ТАК, КАК ОНО БЫВАЕТ: падает сама отправка (нет прав на
    # удалённый), коммит и тег уже сделаны локально. Прежняя редакция пробы
    # сносила и пересоздавала удалённый ПОСЛЕ успешной публикации — тег при
    # этом существовал, номер рос, и проба зеленела, пока боевой путь стоял
    # намертво (ревью кода 12.09.2026, R-4).
    proba_dvor "$T/повтор"
    chmod -R a-w "$T/повтор/удалённый.git"
    OUT=$(proba_publ "$T/повтор"); RC=$?
    NET_TEGA=$(git -C "$T/повтор/удалённый.git" tag -l 'v*' | tr -d '[:space:]')
    if [ "$RC" != 0 ] && [ "$(kommitov "$T/повтор/удалённый.git")" = "0" ] && [ -z "$NET_TEGA" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: отправка не прошла — отказ, наружу не уехало ничего"
    else
        echo "  ПЛОХО упавшая отправка не дала отказа: код $RC, ${OUT:0:100}"; ok=0
    fi
    # Права вернулись: повтор обязан ДОЖАТЬ тот же релиз — тем же номером.
    chmod -R u+w "$T/повтор/удалённый.git"
    OUT=$(proba_publ "$T/повтор"); RC=$?
    TEGI_POVTOR=$(git -C "$T/повтор/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    VERS_POVTOR=$(tr -d '[:space:]' < "$T/повтор/сборка/VERSION" 2>/dev/null)
    if [ "$RC" = 0 ] && [ "$(kommitov "$T/повтор/удалённый.git")" -ge 1 ] \
       && printf '%s' "$TEGI_POVTOR" | grep -q "v$VERS_POVTOR"; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: повтор после упавшей отправки дожал релиз v$VERS_POVTOR"
    else
        echo "  ПЛОХО повтор не отправил: код $RC, коммитов $(kommitov "$T/повтор/удалённый.git"), теги «$TEGI_POVTOR», версия «$VERS_POVTOR», ${OUT:0:90}"; ok=0
    fi

    # БОЛЬНОЙ: публиковать нечего — в пакете с прошлого релиза ни одного
    # изменения. Второй прогон обязан промолчать, а не выпустить призрачный
    # релиз с новым номером и строкой «Изменений: 0» (ревью кода, R-3).
    proba_dvor "$T/призрак"
    proba_publ "$T/призрак" >/dev/null 2>&1
    TEGI_BYLO=$(git -C "$T/призрак/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    BYLO=$(kommitov "$T/призрак/удалённый.git")
    OUT=$(proba_publ "$T/призрак"); RC=$?
    TEGI_STALO=$(git -C "$T/призрак/удалённый.git" tag -l 'v*' | tr '\n' ' ')
    if [ "$RC" = 0 ] && [ "$(kommitov "$T/призрак/удалённый.git")" = "$BYLO" ] \
       && [ "$TEGI_STALO" = "$TEGI_BYLO" ] \
       && printf '%s' "$OUT" | grep -q 'публиковать нечего'; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: изменений нет — второго релиза не выпущено (теги $TEGI_STALO)"
    else
        echo "  ПЛОХО призрачный релиз: код $RC, теги было «$TEGI_BYLO» стало «$TEGI_STALO», ${OUT:0:90}"; ok=0
    fi

    # БОЛЬНОЙ: в пакете есть что выпускать, а запись простым языком не
    # написана — сверху та же, что ушла прошлым релизом. Отказ замком 6в,
    # наружу второго коммита нет.
    proba_dvor "$T/беззаписи"
    proba_publ "$T/беззаписи" >/dev/null 2>&1
    BYLO=$(kommitov "$T/беззаписи/удалённый.git")
    printf 'ещё\n' > "$T/беззаписи/пакет/новое.txt"
    git -C "$T/беззаписи/пакет" add -A
    git -C "$T/беззаписи/пакет" -c user.name=t -c user.email=t@t commit -qm ещё
    OUT=$(proba_publ "$T/беззаписи"); RC=$?
    if [ "$RC" != 0 ] && [ "$BYLO" = 1 ] && printf '%s' "$OUT" | grep -q 'уже выходила' \
       && [ "$(kommitov "$T/беззаписи/удалённый.git")" = "$BYLO" ]; then
        echo "  ок    БОЛЬНОЙ СЛУЧАЙ: релиз без новой записи простым языком — отказ, наружу не уехало"
    else
        echo "  ПЛОХО релиз без записи не остановлен: код $RC, коммитов было $BYLO, ${OUT:0:120}"; ok=0
    fi
    rm -rf "$T"

    PUTEJ=$(grep -c '^\s*echo "  ок    ' "$0" || true)
    [ "$ok" = 1 ] && { echo "САМОТЕСТ ПРОЙДЕН: $PUTEJ путей, первым — больной случай"; exit 0; }
    echo "САМОТЕСТ ПРОВАЛЕН"; exit 1
fi

# Код 2 = «не настроено», код 1 = «замок отказал». Разные вещи: первое чинит
# установка, второе — владелец. «${X:?}» даёт 1 на оба случая, поэтому явно.
if [ -z "${PUBLIC_BRANCH:-}" ] || [ -z "${PUBLIC_REMOTE:-}" ]    || [ -z "${PUBLIC_BUILD_DIR:-}" ] || [ -z "${PUBLIC_COMMIT_EMAIL:-}" ]; then
    echo "не настроено: в harness.conf пусты PUBLIC_REMOTE, PUBLIC_BRANCH, PUBLIC_BUILD_DIR и/или PUBLIC_COMMIT_EMAIL" >&2
    exit 2
fi

# Перепись истории — отдельный режим ДО обычной цепочки замков: ворота
# обычного пути в него не входят, у двери свои проверки.
if [ "${1:-}" = "--переписать-историю" ]; then
    [ "$#" = 4 ] && [ "${3:-}" = "--бэкап" ] || { say "ОТКАЗ: нужно --переписать-историю <каталог-копии> --бэкап <файл.bundle>"; exit 1; }
    perepisat_istoriyu "${2:-}" "${4:-}" || exit 1
    exit 0
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

say "=== публикация: сборка → $PUBLIC_REMOTE/$PUBLIC_BRANCH ==="

# ── Замок 1: ворота ────────────────────────────────────────────────────────
# Свежесть согласия спрашивается ДО ворот (не тратя «да»), а тратится после:
# ворота идут 10–40 минут, и «да», полученное перед запуском, к замку 2 иначе
# старело. Прогон ворот — часть той же попытки, поэтому возраст судится на
# момент её начала.
POPYTKA_NACHATA=$(date +%s)
# Нет кнопки — ворота просят её сами (--просить), но не в проверке: --проверка сеть и владельца не трогает.
PREFLIGHT_ASK=""
[ "$CHECK_ONLY" = 1 ] || PREFLIGHT_ASK="--просить"
PREFLIGHT_REFUSAL=$(HARNESS_LOG_DIR="$LOG_DIR" python3 -I "$SCRIPTS_REAL/deploy_guard.py" --действие публикация --только-согласие \
        --публикация "$PACKAGE_DIR" $PREFLIGHT_ASK 2>&1) || {
    say "ОТКАЗ: $PREFLIGHT_REFUSAL"
    exit 1
}
zamok_vorot "${LOG_DIR:?пуст LOG_DIR — не прочитан паспорт установки}" || exit 1

# ── Замок 2: согласие владельца ИМЕННО на публикацию ───────────────────────
# Судит КОД ВОЗВРАТА ворот: 0 — согласие есть, любой другой (1, 2, упавший python) — стоп.
# Суд по тексту ответа пропускал всякий отказ, сказанный другими словами.
PAKET_SHA=$(git -C "$PACKAGE_DIR" rev-parse HEAD 2>/dev/null) \
    || { say "ОТКАЗ: HEAD пакета $PACKAGE_DIR не прочитан — публиковать нечего"; exit 1; }
if ! REFUSAL=$(HARNESS_LOG_DIR="$LOG_DIR" python3 -I "$SCRIPTS_REAL/deploy_guard.py" --действие публикация --только-согласие \
        --публикация "$PACKAGE_DIR" --свежесть-на "$POPYTKA_NACHATA" 2>&1); then
    say "ОТКАЗ: $REFUSAL"
    exit 1
fi
# Предмет, по которому открыт замок, ворота печатают строкой «предмет: …»; перед пушем он пересчитывается.
PREDMET_ZAMKA=$(printf '%s\n' "$REFUSAL" | sed -n 's/^предмет: //p' | tail -1)
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
if git -C "${PUBLIC_BUILD_DIR:-/нет}" remote get-url "$PUBLIC_REMOTE" >/dev/null 2>&1; then
    OTKUDA_VETKA="$PUBLIC_BUILD_DIR"
fi
git -C "$OTKUDA_VETKA" fetch "$PUBLIC_REMOTE" 2>/dev/null || true
SINCE=$(git -C "$OTKUDA_VETKA" log -1 --format=%cI "$PUBLIC_REMOTE/$PUBLIC_BRANCH" 2>/dev/null) || true
if [ -z "$SINCE" ] && [ "$FIRST" = 0 ]; then
    say "ОТКАЗ: публичной ветки «$PUBLIC_BRANCH» ещё нет — первую точку отсечки называет владелец"
    say "Владелец сказал публиковать как есть? Тогда: bash scripts/publish.sh --первая"
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
if printf '%s' "$PENDING" | grep -q "ОТКАЗ-ИСТОРИИ"; then
    say "ОТКАЗ: историю проекта прочитать не удалось — замку судить не по чему"
    exit 2
fi
if printf '%s' "$PENDING" | grep -q "НЕТ-PyYAML"; then
    say "ОТКАЗ: нет PyYAML — статусы задач проверить нечем (замок судить не может)"
    exit 2
fi
if [ -n "$PENDING" ] && [ "$PO_SLOVU" = 0 ] && propustit_statusy_po_slovu "$LOG_DIR" "$PENDING"; then
    PENDING=""
fi
if [ -n "$PENDING" ] && [ "$PO_SLOVU" = 1 ]; then
    say "ПО СЛОВУ ВЛАДЕЛЬЦА: уезжает непроверенное им — $(printf '%s\n' "$PENDING" | grep -c .) задач(и):"
    printf '%s\n' "$PENDING" | sed 's/^/  • /'
    SPISOK=$(printf '%s\n' "$PENDING" | sed 's/ (статус.*//' | paste -sd', ')
    bash "$SCRIPTS_REAL/tg_send.sh" "Публикую по твоему слову. В публичную ветку уезжает непроверенное тобой: $SPISOK" >/dev/null 2>&1 || true
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
    опубликовать_сборку --сухо || exit 1
    exit 0
fi

# ── Публикация: собранное дерево, а не пакет ───────────────────────────────
опубликовать_сборку || exit 1

