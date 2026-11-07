#!/usr/bin/env bash
# Проба демона «память в гит»: свой репозиторий, свой канал, своя доска.
#
# Откуда взята. Владелец 27.09.2026 о сообщении демона: «И написано
# "разберусь". В итоге это доходит то того что разбираешься?» — не доходило:
# за словом не стояло ни задачи, ни второй попытки, и если смена не читала
# канал, не разбирался никто. Вторая попытка при этом ЕСТЬ — демон ходит
# каждые десять минут, — но владельцу об этом не говорили.
#
# Проверка СОЗДАЁТ своё условие: подставной репозиторий с заведомо красным
# pre-commit, свой LOG_DIR и подменённый tg_send.sh. Боевое не трогаем
# (память: проба-играет-на-боевом).
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Дверь мутаций: копия демона под переменной, боевой файл не трогаем.
DEMON="${MEMORY_COMMIT_DEMON:-$HERE/../harness/demons/memory-commit.sh}"
source "$HERE/lib/selftest_cases.sh"

STAND=$(mktemp -d)
trap 'rm -rf "$STAND"' EXIT

PROJECT="$STAND/proekt"
mkdir -p "$PROJECT/scripts" "$PROJECT/память" "$STAND/zhurnal" "$STAND/doska"
cp "$HERE/board.py" "$HERE/pause.py" "$PROJECT/scripts/"
mkdir -p "$PROJECT/scripts/lib" && cp "$HERE/lib/config.py" "$PROJECT/scripts/lib/"

# Паспорт стенда: демон читает PROJECT_DIR и LOG_DIR ИЗ ФАЙЛА, а не из
# окружения (так он и задуман: cron окружения не даёт). Проба, которая задала
# бы их только переменными, судила бы БОЕВУЮ установку — или, как вышло в
# первой редакции, не запустила бы демона вовсе.
cat > "$STAND/install.conf" <<CONF
PROJECT_DIR="$PROJECT"
LOG_DIR="$STAND/zhurnal"
HEARTBEAT_DIR="$STAND/zhurnal/heartbeat"
CONF

# Порог неудач подряд приходит из harness.conf стенда, а НЕ из окружения:
# прежде проба задавала его переменной, и ключ конфига оставался мёртвым — его
# никто не читал (находка ревью M-09).
# Порог занятых индексов — СВОЙ ключ и нарочно не равен порогу гейтов: код,
# взявший для блокировки порог гейтов, сказал бы владельцу на третьем обходе,
# и проба это видит.
cat > "$STAND/harness.conf" <<CONF
MEMORY_COMMIT_FAILS_MAX=3
MEMORY_COMMIT_LOCK_MAX=5
CONF

cat > "$PROJECT/scripts/tg_send.sh" <<'KANAL'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$LOG_DIR/kanal.txt"
KANAL
chmod +x "$PROJECT/scripts/tg_send.sh"

git -C "$PROJECT" init -q
git -C "$PROJECT" config user.email "proba@stend"
git -C "$PROJECT" config user.name "проба"
printf 'начало\n' > "$PROJECT/README.md"
# Запись памяти ОТСЛЕЖИВАЕТСЯ с самого начала: демон коммитит ПРАВКИ записей,
# сделанные с панели, а `git commit -o память` на неотслеживаемом файле
# отказывает с «pathspec did not match» — это другой отказ, не гейт.
printf 'первая редакция\n' > "$PROJECT/память/запись.md"
git -C "$PROJECT" add -A
git -C "$PROJECT" commit -q -m "начало"

# Красный pre-commit: демон обязан увидеть ИМЕННО отказ гейта, а не пустой набор.
krasnyj_gejt() {
    cat > "$PROJECT/.git/hooks/pre-commit" <<'HOOK'
#!/usr/bin/env bash
echo "[pre-commit] GATE FAIL: проба_гейта"
exit 1
HOOK
    chmod +x "$PROJECT/.git/hooks/pre-commit"
}
zelenyj_gejt() { rm -f "$PROJECT/.git/hooks/pre-commit"; }

progon() {
    env LOG_DIR="$STAND/zhurnal" PROJECT_DIR="$PROJECT" \
        HEARTBEAT_DIR="$STAND/zhurnal/heartbeat" \
        HARNESS_DOSKA="$STAND/doska/доска.json" \
        MEMORY_SUBDIR="память" \
        HARNESS_PANEL_STATE="$STAND/panel-state" \
        HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        bash "$DEMON" > "${1:-/dev/null}" 2>&1
}

skazano() { grep -c . "$STAND/zhurnal/kanal.txt" 2>/dev/null || echo 0; }
# board.py на отсутствующей записи печатает «записи нет» — НЕПУСТУЮ строку.
# Первая редакция пробы судила по пустоте вывода и потому считала снятый долг
# висящим: проверка обязана читать ОТВЕТ, а не длину вывода.
doska_est() {
    # Ответ читается в ПЕРЕМЕННУЮ, а не через конвейер: под `set -o pipefail`
    # код доски (1 на отсутствующей записи) побеждает успешный grep, и проба
    # уходила в «да» при пустой доске — то есть врала ровно там, где судила.
    local otvet
    otvet=$(HARNESS_DOSKA="$STAND/doska/доска.json" \
            python3 "$PROJECT/scripts/board.py" --читать память-не-коммитится 2>/dev/null || true)
    case "$otvet" in
        *"записи нет"*) echo нет ;;
        "")             echo "доска не читается" ;;
        *)              echo да ;;
    esac
}

echo "── БОЛЬНОЙ СЛУЧАЙ 27.09: первый отказ говорит о ВТОРОЙ ПОПЫТКЕ, а не «разберусь» ──"
krasnyj_gejt
printf 'правка с панели\n' > "$PROJECT/память/запись.md"
progon
case_check "владельцу сказано один раз" "1" "$(skazano)"
case_check "слова «разберусь» в сообщении нет" \
    "нет" "$(grep -q 'разберусь' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"
case_check "сказано, что демон повторит сам" \
    "да" "$(grep -q 'повторю сам' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"
case_check "красный гейт назван по имени" \
    "да" "$(grep -q 'проба_гейта' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"

echo "── тот же отказ на следующем обходе: владельцу не пишем ──"
progon
case_check "второго сообщения нет — обходы не спамят" "1" "$(skazano)"

echo "── отказ держится дольше порога: долг на доску и одно слово владельцу ──"
progon
case_check "о затянувшемся отказе сказано ОДИН раз" "2" "$(skazano)"
case_check "долг записан на доску — смена увидит его в стартовом отчёте" \
    "да" "$(doska_est)"
progon
case_check "и на следующем обходе второй раз не повторяем" "2" "$(skazano)"

echo "── гейт починился: долг снят, счёт обнулён ──"
zelenyj_gejt
progon
case_check "об успехе владельцу не пишем" "2" "$(skazano)"
case_check "запись о долге с доски снята" \
    "нет" "$(doska_est)"
case_check "счёт неудач подряд обнулён" \
    "нет" "$([ -f "$STAND/zhurnal/memory-commit.неудач" ] && echo да || echo нет)"
# quotepath=false: без него git печатает кириллицу escape-байтами, и проба
# искала бы слово, которого в выводе нет (память: quotepath).
case_check "правка памяти в итоге закоммичена" \
    "да" "$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%s | grep -q 'Правка памяти' && echo да || echo нет)"

echo "── решение «коммитить нечего» отличает исчезнувшую правку от красного гейта ──"
# Находка ревью M-08: инвариант «нечего коммитить — владельцу не писать» был без
# пробы, и мутация «if false» оставляла стенд зелёным. Судим РЕШЕНИЕ той же
# функцией, что зовёт бой: подсовываем ей оба вывода git.
# Источник функции — САМ демон: `source` его целиком запустил бы работу, поэтому
# берём текст функции (её границы в файле однозначны) и зовём ту же функцию.
sed -n '/^nechego_kommitit() {/,/^}/p' "$DEMON" > "$STAND/reshenie.sh"
NECHEGO=$(bash -c ". '$STAND/reshenie.sh'
    nechego_kommitit 'nothing to commit, working tree clean' && echo да || echo нет")
GEJT=$(bash -c ". '$STAND/reshenie.sh'
    nechego_kommitit '[pre-commit] GATE FAIL: проба_гейта' && echo да || echo нет")
case_check "вывод «nothing to commit» признан исчезнувшей правкой" "да" "$NECHEGO"
case_check "БОЛЬНОЙ СЛУЧАЙ: красный гейт исчезнувшей правкой НЕ считается" "нет" "$GEJT"

echo "── БОЛЬНОЙ СЛУЧАЙ 28.09: НОВАЯ запись памяти обязана попасть в коммит ──"
# Коммит bf91834 назвал в сообщении MEMORY.md и вторую запись, а внёс один
# MEMORY.md: `git commit --only <путь>` берёт ТОЛЬКО отслеживаемые пути. Прежний
# стенд этого не видел — запись в нём была отслеженной «с самого начала», то
# есть слепое место было ВПИСАНО в пробу.
printf 'вторая редакция\n' > "$PROJECT/память/запись.md"
printf 'новая запись\n' > "$PROJECT/память/проба-новой-записи.md"
progon
SOSTAV="$(git -C "$PROJECT" -c core.quotepath=false show --name-only --format= HEAD 2>/dev/null || true)"
case_check "новая (неотслеженная) запись попала в коммит" \
    "да" "$(printf '%s\n' "$SOSTAV" | grep -q 'проба-новой-записи.md' && echo да || echo нет)"
case_check "правка старой записи тоже в коммите" \
    "да" "$(printf '%s\n' "$SOSTAV" | grep -q 'запись.md' && echo да || echo нет)"
case_check "сообщение коммита называет новую запись" \
    "да" "$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%s | grep -q 'проба-новой-записи.md' && echo да || echo нет)"
case_check "после коммита в памяти не осталось неотслеженного" \
    "" "$(git -C "$PROJECT" status --porcelain -- память 2>/dev/null || true)"
# Сверка состава судится по СВОЕМУ следу: демон пишет в этот файл имена,
# которые назвал и не внёс. Первая редакция пробы искала в нём слово «БЕДА» —
# а оно уходит в вывод демона, не в файл: под мутацией проба оставалась
# зелёной и сверку не доказывала.
case_check "сверка состава: названных и не внесённых файлов нет" \
    "0" "$(grep -c . "$STAND/zhurnal/memory-commit.вывод.назвал-не-внёс" 2>/dev/null || true)"

gejt_s_imenem() {  # $1 — имя красного гейта
    printf '#!/usr/bin/env bash\necho "[pre-commit] GATE FAIL: %s"\nexit 1\n' "$1" \
        > "$PROJECT/.git/hooks/pre-commit"
    chmod +x "$PROJECT/.git/hooks/pre-commit"
}
schet() { cat "$STAND/zhurnal/memory-commit.неудач" 2>/dev/null || echo 0; }
# Счёт занятых индексов — свой файл: в общий счёт гейтов блокировка не идёт.
schet_zamkov() { cat "$STAND/zhurnal/memory-commit.индекс-занят" 2>/dev/null || echo 0; }
doska_tekst() {
    HARNESS_DOSKA="$STAND/doska/доска.json" \
        python3 "$PROJECT/scripts/board.py" --читать память-не-коммитится 2>/dev/null || true
}
doska_zamka() {
    local otvet
    otvet=$(HARNESS_DOSKA="$STAND/doska/доска.json" \
            python3 "$PROJECT/scripts/board.py" --читать память-индекс-занят 2>/dev/null || true)
    case "$otvet" in
        *"записи нет"*) echo нет ;;
        "")             echo "доска не читается" ;;
        *)              printf '%s' "$otvet" | grep -q 'индекс занят' && echo да || echo "чужой текст" ;;
    esac
}
puls() { [ -f "$STAND/zhurnal/heartbeat/memory-commit" ] && echo да || echo нет; }

echo "── БОЛЬНОЙ СЛУЧАЙ 02.11: занятый индекс — не красный гейт ──"
# Блокировка НАСТОЯЩАЯ: git сам отказывает своим текстом, проба не сочиняет вывод.
printf 'третья редакция\n' > "$PROJECT/память/запись.md"
gejt_s_imenem gejt_odin
progon
SKAZANO_DO=$(skazano)
case_check "первый отказ гейта посчитан" "1" "$(schet)"
: > "$PROJECT/.git/index.lock"
rm -f "$STAND/zhurnal/heartbeat/memory-commit"
progon
rm -f "$PROJECT/.git/index.lock"
case_check "вывод коммита — отказ блокировки индекса" \
    "да" "$(grep -q 'index.lock' "$STAND/zhurnal/memory-commit.вывод" && echo да || echo нет)"
case_check "занятый индекс счёт неудач не двигает" "1" "$(schet)"
case_check "о занятом индексе владельцу не пишем" "$SKAZANO_DO" "$(skazano)"
case_check "метка пульса стоит — обход состоялся" \
    "да" "$([ -f "$STAND/zhurnal/heartbeat/memory-commit" ] && echo да || echo нет)"

echo "── серия из разных гейтов: владелец и доска слышат ВСЕ ──"
gejt_s_imenem gejt_dva
progon
progon
case_check "порог достигнут" "3" "$(schet)"
POSLEDNEE=$(tail -1 "$STAND/zhurnal/kanal.txt" 2>/dev/null || true)
case_check "сообщение о пороге называет первый гейт серии" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -q 'gejt_odin' && echo да || echo нет)"
case_check "сообщение о пороге называет второй гейт серии" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -q 'gejt_dva' && echo да || echo нет)"
case_check "доска называет оба гейта" \
    "да" "$(doska_tekst | grep -q 'gejt_odin' && doska_tekst | grep -q 'gejt_dva' && echo да || echo нет)"
# Повтор гейта в серии — одно имя: «gejt_dva, gejt_dva» владелец прочёл бы как
# два разных отказа. Занятый индекс в серии не участвует — его текст гейтом не назван.
case_check "гейт, отказавший дважды, назван один раз" \
    "1" "$(printf '%s' "$POSLEDNEE" | grep -o 'gejt_dva' | grep -c . || true)"
case_check "отказ блокировки индекса гейтом не назван" \
    "нет" "$(printf '%s' "$POSLEDNEE" | grep -q 'index.lock\|fatal' && echo да || echo нет)"

echo "── удача снимает имена вместе со счётом ──"
zelenyj_gejt
progon
gejt_s_imenem gejt_tri
printf 'четвёртая редакция\n' > "$PROJECT/память/запись.md"
progon; progon; progon
POSLEDNEE=$(tail -1 "$STAND/zhurnal/kanal.txt" 2>/dev/null || true)
case_check "новая серия называет свой гейт" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -q 'gejt_tri' && echo да || echo нет)"
case_check "гейты прошлой серии в новой не названы" \
    "нет" "$(printf '%s' "$POSLEDNEE" | grep -q 'gejt_odin\|gejt_dva' && echo да || echo нет)"
case_check "серия из одного гейта называет его один раз" \
    "1" "$(printf '%s' "$POSLEDNEE" | grep -o 'gejt_tri' | grep -c . || true)"

echo "── серия занятых индексов до своего порога: ни счёта гейтов, ни тревоги, ни долга ──"
# Живая серия 02.11: index.lock шёл в счёт наравне с гейтами и вёл к тревоге
# о «красном гейте», которого не было. Обходов больше порога (3) — иначе
# мутация «блокировка считается» не дошла бы до тревоги и осталась бы незамеченной.
zelenyj_gejt
progon
SKAZANO_DO=$(skazano)
printf 'пятая редакция\n' > "$PROJECT/память/запись.md"
: > "$PROJECT/.git/index.lock"
PULS_VSE=да
for _ in 1 2 3 4; do
    rm -f "$STAND/zhurnal/heartbeat/memory-commit"
    progon
    [ -f "$STAND/zhurnal/heartbeat/memory-commit" ] || PULS_VSE=нет
done
rm -f "$PROJECT/.git/index.lock"
case_check "серия занятых индексов счёт не растит" "0" "$(schet)"
case_check "о серии занятых индексов владельцу не пишем" "$SKAZANO_DO" "$(skazano)"
case_check "долга на доске от занятого индекса нет" "нет" "$(doska_est)"
case_check "метка пульса стоит после каждого обхода с занятым индексом" "да" "$PULS_VSE"
case_check "занятые индексы считаются своим счётом" "4" "$(schet_zamkov)"

echo "── застрявший индекс виден: по своему порогу — слово владельцу и долг ──"
# Блокировка, которую никто не снимет, иначе молчала бы вечно: правка памяти
# не в гите, а сторож здоров по метке. Порог занятых индексов на стенде 5.
: > "$PROJECT/.git/index.lock"
rm -f "$STAND/zhurnal/heartbeat/memory-commit"
progon
SOOBSHCHENIE=$(tail -1 "$STAND/zhurnal/kanal.txt" 2>/dev/null || true)
case_check "на пятом занятом обходе владельцу сказано один раз" "$((SKAZANO_DO + 1))" "$(skazano)"
case_check "сообщение называет беду: «индекс занят 5 обходов»" \
    "да" "$(printf '%s' "$SOOBSHCHENIE" | grep -qF 'индекс занят 5 обходов' && echo да || echo нет)"
case_check "занятый индекс красным гейтом не назван" \
    "нет" "$(printf '%s' "$SOOBSHCHENIE" | grep -q 'красный гейт' && echo да || echo нет)"
case_check "долг занятого индекса на доске" "да" "$(doska_zamka)"
case_check "долга красного гейта от занятого индекса нет" "нет" "$(doska_est)"
case_check "счёт гейтов и на пороге блокировок пуст" "0" "$(schet)"
case_check "метка пульса стоит на пороге блокировок" "да" "$(puls)"
progon; progon; progon
case_check "за порогом блокировок владельцу не повторяем" "$((SKAZANO_DO + 1))" "$(skazano)"
case_check "счёт занятых индексов идёт дальше" "8" "$(schet_zamkov)"
rm -f "$PROJECT/.git/index.lock"

echo "── индекс освободился: счёт занятых индексов и долг сняты ──"
progon
case_check "правка, ждавшая индекса, закоммичена" \
    "да" "$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%s | grep -q 'Правка памяти' && echo да || echo нет)"
case_check "зелёный обход снимает счёт занятых индексов" \
    "нет" "$([ -f "$STAND/zhurnal/memory-commit.индекс-занят" ] && echo да || echo нет)"
case_check "зелёный обход снимает долг занятого индекса" "нет" "$(doska_zamka)"
case_check "об освобождении владельцу не пишем" "$((SKAZANO_DO + 1))" "$(skazano)"

echo "── X1 отказ хука без «GATE FAIL»: сторож не падает, считает и извещает ──"
# Пустой разбор имени гейта под set -e с pipefail ронял сторожа до счёта.
zelenyj_gejt; printf 'x1\n' > "$PROJECT/память/запись.md"; progon
SK=$(skazano)
printf '#!/usr/bin/env bash\necho boom\nexit 1\n' > "$PROJECT/.git/hooks/pre-commit"
chmod +x "$PROJECT/.git/hooks/pre-commit"
printf 'x1b\n' > "$PROJECT/память/запись.md"
rm -f "$STAND/zhurnal/heartbeat/memory-commit"; progon
case_check "X1 отказ без имени посчитан" "1" "$(schet)"
case_check "X1 метка пульса стоит" "да" "$(puls)"
case_check "X1 первый отказ назван владельцу" "$((SK + 1))" "$(skazano)"
progon; progon
case_check "X1 порог достигнут" "3" "$(schet)"
case_check "X1 по порогу владелец извещён второй раз" "$((SK + 2))" "$(skazano)"
case_check "X1 долг на доске" "да" "$(doska_est)"

echo "── X2 красный гейт, в чьём тексте есть index.lock — гейт, а не занятый индекс ──"
zelenyj_gejt; printf 'x2\n' > "$PROJECT/память/запись.md"; progon
SK=$(skazano)
gejt_s_imenem "stale_lock найден .git/index.lock"
printf 'x2b\n' > "$PROJECT/память/запись.md"
progon; progon; progon
POSLEDNEE=$(tail -1 "$STAND/zhurnal/kanal.txt" 2>/dev/null || true)
case_check "X2 отказ гейта посчитан до порога" "3" "$(schet)"
case_check "X2 первое слово и тревога по порогу" "$((SK + 2))" "$(skazano)"
case_check "X2 тревога называет гейт" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -q 'stale_lock' && echo да || echo нет)"
case_check "X2 тревога — о красном гейте, не о занятом индексе" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -q 'красный гейт' && ! printf '%s' "$POSLEDNEE" | grep -q 'индекс занят' && echo да || echo нет)"
case_check "X2 долг на доске" "да" "$(doska_est)"
case_check "X2 счёт занятых индексов не тронут" "0" "$(schet_zamkov)"

echo "── X3 кириллица в имени гейта под LC_ALL=C ──"
zelenyj_gejt; printf 'x3\n' > "$PROJECT/память/запись.md"; progon
gejt_s_imenem "гейт_кириллица"; printf 'x3b\n' > "$PROJECT/память/запись.md"
LC_ALL=C progon; LC_ALL=C progon
case_check "X3 имя в канале" \
    "да" "$(tail -1 "$STAND/zhurnal/kanal.txt" | grep -q 'гейт_кириллица' && echo да || echo нет)"
case_check "X3 имя в списке серии один раз" \
    "1" "$(grep -c . "$STAND/zhurnal/memory-commit.неудач.gates" 2>/dev/null || true)"

echo "── X4 список гейтов без прав на чтение: сторож не падает ──"
chmod 000 "$STAND/zhurnal/memory-commit.неудач.gates"
rm -f "$STAND/zhurnal/heartbeat/memory-commit"; progon
chmod 644 "$STAND/zhurnal/memory-commit.неудач.gates"
case_check "X4 метка пульса стоит" "да" "$(puls)"
case_check "X4 счёт растёт" "3" "$(schet)"

echo "── R-4 отказ без имени гейта не наследует имена прежней серии ──"
# Счёт пропал, список имён остался: новая серия обязана начаться с чистого листа.
zelenyj_gejt; printf 'r4\n' > "$PROJECT/память/запись.md"; progon
gejt_s_imenem gejt_staryj; printf 'r4b\n' > "$PROJECT/память/запись.md"; progon
rm -f "$STAND/zhurnal/memory-commit.неудач"
printf '#!/usr/bin/env bash\necho boom\nexit 1\n' > "$PROJECT/.git/hooks/pre-commit"
progon
POSLEDNEE=$(tail -1 "$STAND/zhurnal/kanal.txt" 2>/dev/null || true)
case_check "R-4 новая серия начата заново" "1" "$(schet)"
case_check "R-4 первое слово новой серии без имени прежней" \
    "нет" "$(printf '%s' "$POSLEDNEE" | grep -q 'gejt_staryj' && echo да || echo нет)"

echo "── R-5 повреждённый счёт сбрасывает и список гейтов ──"
zelenyj_gejt; printf 'r5\n' > "$PROJECT/память/запись.md"; progon
gejt_s_imenem gejt_staryj; printf 'r5b\n' > "$PROJECT/память/запись.md"; progon
printf 'мусор\n' > "$STAND/zhurnal/memory-commit.неудач"
gejt_s_imenem gejt_novyj
progon
POSLEDNEE=$(tail -1 "$STAND/zhurnal/kanal.txt" 2>/dev/null || true)
case_check "R-5 мусор в счёте — серия с единицы" "1" "$(schet)"
case_check "R-5 список гейтов серии — только новый гейт" \
    "gejt_novyj" "$(cat "$STAND/zhurnal/memory-commit.неудач.gates" 2>/dev/null || true)"
case_check "R-5 сообщение без имени прежней серии" \
    "нет" "$(printf '%s' "$POSLEDNEE" | grep -q 'gejt_staryj' && echo да || echo нет)"
case_check "R-5 сообщение называет новый гейт" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -q 'gejt_novyj' && echo да || echo нет)"

echo "── W1 прополка запускается при правах 644 (без бита запуска) ──"
# Заглушка вместо настоящей прополки: важен сам запуск, а не её работа.
zelenyj_gejt
printf '#!/usr/bin/env python3\nprint("прополка-запущена")\n' > "$PROJECT/scripts/weed-the-orders.py"
chmod 644 "$PROJECT/scripts/weed-the-orders.py"
printf 'w1\n' > "$PROJECT/память/запись.md"
progon "$STAND/w1.out"
case_check "W1 прополка с правами 644 запущена" \
    "да" "$(grep -q 'прополка-запущена' "$STAND/w1.out" && echo да || echo нет)"
rm -f "$PROJECT/scripts/weed-the-orders.py"
progon "$STAND/w2.out"
case_check "W2 прополки нет — об этом сказано" \
    "да" "$(grep -q 'прополки нет' "$STAND/w2.out" && echo да || echo нет)"

printf '\nПАМЯТЬ-В-ГИТ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
