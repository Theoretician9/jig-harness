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
# Владелец 16.11 о сообщении «Правку памяти с панели пока не сохранил…: красный
# гейт «прополка_без_потерь»»: «А что за правка памяти, что туда сохранилось» —
# панелью он ничего не правил, а имя гейта ему ничего не говорит. Канал называет
# ФАЙЛ человеческими словами и причину одной фразой; имя гейта — смене, на доску.
case_check "в канале нет имени гейта" \
    "нет" "$(grep -q 'проба_гейта' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"
case_check "в канале нет слова «гейт»" \
    "нет" "$(grep -q 'гейт' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"
case_check "в канале нет «с панели» — источник демону не известен" \
    "нет" "$(grep -q 'с панели' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"
case_check "сообщение называет файл человеческими словами" \
    "да" "$(grep -qF 'запись памяти (запись.md)' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"
case_check "причина названа простыми словами" \
    "да" "$(grep -qF 'не пропустила проверка перед коммитом' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)"

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
    "да" "$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%s | grep -q '^Сохранил в git файлы памяти: ' && echo да || echo нет)"

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
case_check "сообщение о пороге имён гейтов владельцу не несёт" \
    "нет" "$(printf '%s' "$POSLEDNEE" | grep -q 'gejt_odin\|gejt_dva' && echo да || echo нет)"
case_check "доска называет оба гейта" \
    "да" "$(doska_tekst | grep -q 'gejt_odin' && doska_tekst | grep -q 'gejt_dva' && echo да || echo нет)"
# Повтор гейта в серии — одно имя: «gejt_dva, gejt_dva» владелец прочёл бы как
# два разных отказа. Занятый индекс в серии не участвует — его текст гейтом не назван.
case_check "гейт, отказавший дважды, назван один раз" \
    "1" "$(doska_tekst | grep -o 'gejt_dva' | grep -c . || true)"
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
    "да" "$(doska_tekst | grep -q 'gejt_tri' && echo да || echo нет)"
case_check "гейты прошлой серии в новой не названы" \
    "нет" "$(doska_tekst | grep -q 'gejt_odin\|gejt_dva' && echo да || echo нет)"
case_check "серия из одного гейта называет его один раз" \
    "1" "$(doska_tekst | grep -o 'gejt_tri' | grep -c . || true)"

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
case_check "сообщение называет беду: «git занят другим процессом 5 обходов»" \
    "да" "$(printf '%s' "$SOOBSHCHENIE" | grep -qF 'git занят другим процессом 5 обходов' && echo да || echo нет)"
case_check "занятый индекс отказом проверки не назван" \
    "нет" "$(printf '%s' "$SOOBSHCHENIE" | grep -q 'проверка перед коммитом' && echo да || echo нет)"
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
    "да" "$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%s | grep -q '^Сохранил в git файлы памяти: ' && echo да || echo нет)"
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
case_check "X2 доска называет гейт" \
    "да" "$(doska_tekst | grep -q 'stale_lock' && echo да || echo нет)"
case_check "X2 тревога — об отказе проверки, не о занятом git" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -q 'проверка перед коммитом' && ! printf '%s' "$POSLEDNEE" | grep -q 'git занят' && echo да || echo нет)"
case_check "X2 долг на доске" "да" "$(doska_est)"
case_check "X2 счёт занятых индексов не тронут" "0" "$(schet_zamkov)"

echo "── X3 кириллица в имени гейта под LC_ALL=C ──"
zelenyj_gejt; printf 'x3\n' > "$PROJECT/память/запись.md"; progon
gejt_s_imenem "гейт_кириллица"; printf 'x3b\n' > "$PROJECT/память/запись.md"
LC_ALL=C progon; LC_ALL=C progon
case_check "X3 имя в списке серии" \
    "да" "$(grep -q 'гейт_кириллица' "$STAND/zhurnal/memory-commit.неудач.gates" 2>/dev/null && echo да || echo нет)"
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

echo "── БОЛЬНОЙ СЛУЧАЙ нового сервера: каталог памяти целиком не в гите ──"
# `git status --porcelain` сворачивает неотслеженный каталог в «?? память/», и
# сверка состава искала в коммите файл с именем каталога — ложная «БЕДА».
zelenyj_gejt
git -C "$PROJECT" rm -r -q --cached память
git -C "$PROJECT" commit -q -m "память убрана из гита (условие пробы)"
printf 'запись нового сервера\n' > "$PROJECT/память/новый-сервер.md"
progon "$STAND/dir-untracked.out"
case_check "каталог целиком неотслеженный: ложной БЕДЫ нет" \
    "0" "$(grep -c 'БЕДА' "$STAND/dir-untracked.out" || true)"
case_check "каталог целиком неотслеженный: запись в коммите" \
    "да" "$(git -C "$PROJECT" -c core.quotepath=false show --name-only --format= HEAD | grep -q 'новый-сервер.md' && echo да || echo нет)"

echo "── БОЛЬНОЙ СЛУЧАЙ 11.11: гонка за HEAD — не красный гейт ──"
# Пока демон ~80 с гоняет гейты, другой процесс коммитит, и git отказывает
# «cannot lock ref 'HEAD'». Гейты при этом прошли: это занятость, как
# index.lock, а не отказ гейта. Гонку не воспроизвести по времени, поэтому
# `git commit` подменяет обёртка в PATH стенда: она печатает точный текст git.
# Остальные команды git идут в настоящий git.
NASTOYASHCHIJ_GIT="$(command -v git)"
mkdir -p "$STAND/bin"
cat > "$STAND/bin/git" <<OBERTKA
#!/usr/bin/env bash
if [ "\${1:-}" = commit ]; then
    echo "fatal: cannot lock ref 'HEAD': is at aaa but expected bbb" >&2
    exit 128
fi
exec "$NASTOYASHCHIJ_GIT" "\$@"
OBERTKA
chmod +x "$STAND/bin/git"
zelenyj_gejt
printf 'гонка за HEAD\n' > "$PROJECT/память/запись.md"
SK=$(skazano)
rm -f "$STAND/zhurnal/heartbeat/memory-commit"
PATH="$STAND/bin:$PATH" progon "$STAND/head-race.out"
case_check "H1 вывод коммита — отказ блокировки HEAD (стенд сработал)" \
    "да" "$(grep -q "cannot lock ref 'HEAD'" "$STAND/zhurnal/memory-commit.вывод" && echo да || echo нет)"
case_check "H1 гонка за HEAD счёт неудач не двигает" "0" "$(schet)"
case_check "H1 о гонке за HEAD владельцу не пишем" "$SK" "$(skazano)"
case_check "H1 гонка за HEAD идёт в счёт занятого git" "1" "$(schet_zamkov)"
case_check "H1 гонка за HEAD гейтом не названа в журнале" \
    "0" "$(grep -c 'коммит НЕ прошёл (гейты)' "$STAND/head-race.out" || true)"
case_check "H1 в журнале сказано о повторе на следующем обходе" \
    "да" "$(grep -q 'повторю на следующем обходе' "$STAND/head-race.out" && echo да || echo нет)"
case_check "H1 метка пульса стоит" "да" "$(puls)"
progon
case_check "H1 без гонки правка закоммичена" \
    "да" "$(git -C "$PROJECT" -c core.quotepath=false show --name-only --format= HEAD | grep -q 'запись.md' && echo да || echo нет)"

echo "── БОЛЬНОЙ СЛУЧАЙ 16.11: файлы механизмов не названы «правкой с панели» ──"
# Коммит 5e58e5ac «Правка памяти с панели: .tasks-triaged, ORDERS.md, …»:
# файлы правили агент и механизмы, а не владелец. Демон источника не знает —
# говорит, ЧТО сохраняет, и человеческими словами.
printf 'указание\n' > "$PROJECT/память/ORDERS.md"
printf 'отметка\n' > "$PROJECT/память/.tasks-triaged"
krasnyj_gejt
progon
POSLEDNEE=$(tail -1 "$STAND/zhurnal/kanal.txt" 2>/dev/null || true)
case_check "Z1 отказ: канал называет указания владельца" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -qF 'указания владельца (ORDERS.md)' && echo да || echo нет)"
case_check "Z1 отказ: канал называет отметки разбора сообщений" \
    "да" "$(printf '%s' "$POSLEDNEE" | grep -qF 'отметки разбора сообщений (.tasks-triaged)' && echo да || echo нет)"
case_check "Z1 отказ: без «с панели»" \
    "нет" "$(printf '%s' "$POSLEDNEE" | grep -q 'панел' && echo да || echo нет)"
zelenyj_gejt
progon
TEMA="$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%s)"
TELO="$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%b)"
case_check "Z2 подпись коммита без «с панели»" \
    "нет" "$(printf '%s\n%s' "$TEMA" "$TELO" | grep -q 'с панели\|через веб-панель' && echo да || echo нет)"
case_check "Z2 подпись называет указания владельца" \
    "да" "$(printf '%s' "$TEMA" | grep -qF 'указания владельца (ORDERS.md)' && echo да || echo нет)"
case_check "Z2 подпись называет отметки разбора сообщений" \
    "да" "$(printf '%s' "$TEMA" | grep -qF 'отметки разбора сообщений (.tasks-triaged)' && echo да || echo нет)"
case_check "Z2 сверка состава: оба файла в коммите" \
    "0" "$(grep -c . "$STAND/zhurnal/memory-commit.вывод.назвал-не-внёс" 2>/dev/null || true)"

# ══ Е: служебные отметки — пачкой, не под замком очереди слияния ════════════
# Спека тяжёлых прогонов, раздел Е, и ревью ЕЖ п. 1, 2, 7. Каждый случай —
# свой проект, свой LOG_DIR и свой файл замка $LOG_DIR/merge-queue.lock (боевой замок
# не трогается). Класс служебных путей — ДАННЫЕ: ключ DAEMON_OWNED_PATHS стенда.
# Время прошлых коммитов задаётся датой коммита, а не ожиданием.
SERVICE_CLASS="память/.tasks-triaged память/.orders-processed память/.details-triaged память/.указания-разобрано docs/приёмка/"

e_stand() {  # $1 — имя случая; ставит EP (проект), EL (журналы), ELOCK (замок очереди)
    local d="$STAND/e/$1"
    EP="$d/proekt"; EL="$d/zhurnal"; ELOCK="$EL/merge-queue.lock"
    mkdir -p "$EP/scripts/lib" "$EP/память" "$EL"
    cp "$HERE/board.py" "$HERE/pause.py" "$EP/scripts/"
    cp "$HERE/lib/config.py" "$EP/scripts/lib/"
    # Единый судья класса путей (ревью ЕЖ п. 6): демон зовёт его из проекта.
    cp "$HERE/service-only-diff.py" "$EP/scripts/" 2>/dev/null \
        || echo "  (судьи класса scripts/service-only-diff.py нет — механизма нет)"
    printf 'PROJECT_DIR="%s"\nLOG_DIR="%s"\nHEARTBEAT_DIR="%s/heartbeat"\n' "$EP" "$EL" "$EL" \
        > "$d/install.conf"
    printf 'MEMORY_COMMIT_FAILS_MAX=3\nMEMORY_COMMIT_LOCK_MAX=5\nDAEMON_OWNED_PATHS="%s"\nMEMORY_SERVICE_COMMIT_MIN=60\nMEMORY_OWNER_WAIT_MIN=30\n' \
        "$SERVICE_CLASS" > "$d/harness.conf"
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$1" >> "$LOG_DIR/kanal.txt"\n' > "$EP/scripts/tg_send.sh"
    chmod +x "$EP/scripts/tg_send.sh"
    git -C "$EP" init -q -b master
    git -C "$EP" config user.email "proba@stend"
    git -C "$EP" config user.name "проба"
    printf 'начало\n' > "$EP/README.md"
    printf 'указание 0\n' > "$EP/память/ORDERS.md"
    printf 'отметка 0\n' > "$EP/память/.tasks-triaged"
    git -C "$EP" add -A
    e_commit_at 180 "начало"
}

e_commit_at() {  # $1 — сколько минут назад, $2 — сообщение; коммитит индекс
    local when
    when="$(date -d "-$1 minutes" -R)"
    GIT_AUTHOR_DATE="$when" GIT_COMMITTER_DATE="$when" \
        git -C "$EP" commit -q --allow-empty -m "$2"
}

e_progon() {  # $1 — файл вывода демона
    local d; d="$(dirname "$EP")"
    env LOG_DIR="$EL" PROJECT_DIR="$EP" HEARTBEAT_DIR="$EL/heartbeat" \
        HARNESS_DOSKA="$d/доска.json" MEMORY_SUBDIR="память" \
        HARNESS_PANEL_STATE="$d/panel-state" \
        HARNESS_INSTALL_CONF="$d/install.conf" HARNESS_CONF="$d/harness.conf" \
        bash "$DEMON" > "${1:-/dev/null}" 2>&1
}

e_head() { git -C "$EP" rev-parse HEAD; }
e_in_head() {  # $1 — путь; да, если он в составе последнего коммита
    git -C "$EP" -c core.quotepath=false show --name-only --format= HEAD | grep -qxF "$1" \
        && echo да || echo нет
}
e_puls() { [ -f "$EL/heartbeat/memory-commit" ] && echo да || echo нет; }

E_HOLDER=""
e_hold_lock() {  # держит flock на ELOCK, пока не снят e_free_lock; ждёт, что замок взят
    flock -x "$ELOCK" sleep 300 &
    E_HOLDER=$!
    local i
    for i in $(seq 1 50); do
        flock -n "$ELOCK" true 2>/dev/null || return 0
        sleep 0.1
    done
    echo "  ПРОБА СЛОМАНА: замок стенда не взят за 5 с"
}
e_free_lock() {
    [ -n "$E_HOLDER" ] || return 0
    pkill -P "$E_HOLDER" 2>/dev/null; kill "$E_HOLDER" 2>/dev/null; wait "$E_HOLDER" 2>/dev/null
    E_HOLDER=""
}

echo "── Е(а) замок очереди занят, изменены только отметки → коммита нет, пульс стоит (Е(е)) ──"
# Поломка: демон не смотрит на замок и сдвигает master посреди слияния (17.11: повтор
# ворот и два отказа кодом 6). Пульс (Е(е)): отложенный обход — состоявшийся обход;
# без метки сторож пульса назвал бы живого демона мёртвым.
e_stand a
printf 'отметка 1\n' >> "$EP/память/.tasks-triaged"
DO=$(e_head)
e_hold_lock
e_progon "$EL/a.out"
e_free_lock
case_check "Е(а) под занятым замком коммита нет" "$DO" "$(e_head)"
case_check "Е(а) правка отметки цела на диске" "да" \
    "$(grep -q 'отметка 1' "$EP/память/.tasks-triaged" && echo да || echo нет)"
case_check "Е(е) отложил — метка пульса стоит" "да" "$(e_puls)"
case_check "Е(а) причина отложенного коммита названа в журнале (замок)" "да" \
    "$(grep -q 'замок\|merge-queue' "$EL/a.out" && echo да || echo нет)"

echo "── Е(б) замок занят, изменены указания владельца → коммита нет ──"
# Поломка: «правка владельца — сразу» понята как «мимо замка»: ORDERS.md так же
# сдвигает master под слиянием, как и отметка.
e_stand b
printf 'указание 1\n' >> "$EP/память/ORDERS.md"
DO=$(e_head)
e_hold_lock
e_progon "$EL/b.out"
e_free_lock
case_check "Е(б) ORDERS.md под занятым замком не закоммичен" "$DO" "$(e_head)"
case_check "Е(б) метка пульса стоит" "да" "$(e_puls)"

echo "── Е(в) файл замка есть, но не занят + ORDERS.md → коммит ──"
# Поломка «проверять существование файла замка»: файл после очереди остаётся всегда,
# и такой демон не коммитил бы никогда — а (а) с такой ломкой осталась бы зелёной.
e_stand v
: > "$ELOCK"
printf 'указание 1\n' >> "$EP/память/ORDERS.md"
DO=$(e_head)
e_progon "$EL/v.out"
case_check "Е(в) свободный замок: коммит есть" "да" "$([ "$(e_head)" != "$DO" ] && echo да || echo нет)"
case_check "Е(в) ORDERS.md в коммите" "да" "$(e_in_head память/ORDERS.md)"

echo "── Е(г) только отметки: коммит класса 10 мин назад → нет, 61 мин → есть ──"
# Поломка: служебные отметки коммитятся каждым обходом (17.11 — 19 коммитов
# .tasks-triaged в 11 разных часах), порог MEMORY_SERVICE_COMMIT_MIN не судится.
e_stand g10
printf 'отметка 1\n' >> "$EP/память/.tasks-triaged"
git -C "$EP" add -- память/.tasks-triaged
e_commit_at 10 "отметки 10 мин назад"
printf 'отметка 2\n' >> "$EP/память/.tasks-triaged"
DO=$(e_head)
rm -f "$EL/heartbeat/memory-commit"
e_progon "$EL/g10.out"
case_check "Е(г) класс закоммичен 10 мин назад — нового коммита нет" "$DO" "$(e_head)"
case_check "Е(е) отложил по сроку — метка пульса стоит" "да" "$(e_puls)"
e_stand g61
printf 'отметка 1\n' >> "$EP/память/.tasks-triaged"
git -C "$EP" add -- память/.tasks-triaged
e_commit_at 61 "отметки 61 мин назад"
printf 'отметка 2\n' >> "$EP/память/.tasks-triaged"
e_progon "$EL/g61.out"
case_check "Е(г) класс закоммичен 61 мин назад — коммит отметок есть" "да" \
    "$(e_in_head память/.tasks-triaged)"

echo "── Е(г') чужой коммит минуту назад, коммит класса 61 мин назад → коммит ──"
# Поломка (ревью ЕЖ п. 7): `git log -1 --format=%ct` без путей класса — это время HEAD;
# любой чужой коммит откладывал бы отметки бесконечно.
e_stand g1
printf 'отметка 1\n' >> "$EP/память/.tasks-triaged"
git -C "$EP" add -- память/.tasks-triaged
e_commit_at 61 "отметки 61 мин назад"
printf 'чужое\n' >> "$EP/README.md"
git -C "$EP" add -- README.md
e_commit_at 1 "чужой коммит минуту назад"
printf 'отметка 2\n' >> "$EP/память/.tasks-triaged"
e_progon "$EL/g1.out"
case_check "Е(г') время берётся по путям класса — отметки закоммичены" "да" \
    "$(e_in_head память/.tasks-triaged)"
# Класс — все пути ключа, а не один изменённый файл: другая отметка класса закоммичена
# 10 мин назад — срок пачки не вышел. Ловит ломку «время по изменённому файлу».
e_stand g2
printf 'разобрано 1\n' > "$EP/память/.orders-processed"
git -C "$EP" add -- память/.orders-processed
e_commit_at 10 "другая отметка класса 10 мин назад"
printf 'отметка 1\n' >> "$EP/память/.tasks-triaged"
DO=$(e_head)
e_progon "$EL/g2.out"
case_check "Е(г') класс — все пути ключа: другая отметка 10 мин назад держит срок" "$DO" "$(e_head)"

echo "── Е(д) ORDERS.md + отметка → один коммит с обоими ──"
# Поломка: правка владельца коммитится одна, а отметки ждут своего часа отдельным
# коммитом — лишний сдвиг master.
e_stand d
printf 'отметка 1\n' >> "$EP/память/.tasks-triaged"
git -C "$EP" add -- память/.tasks-triaged
e_commit_at 5 "отметки 5 мин назад"
printf 'указание 1\n' >> "$EP/память/ORDERS.md"
printf 'отметка 2\n' >> "$EP/память/.tasks-triaged"
DO=$(e_head)
e_progon "$EL/d.out"
case_check "Е(д) ровно один новый коммит" "1" "$(git -C "$EP" rev-list --count "$DO..HEAD")"
case_check "Е(д) ORDERS.md в коммите" "да" "$(e_in_head память/ORDERS.md)"
case_check "Е(д) отметка едет с ним" "да" "$(e_in_head память/.tasks-triaged)"

echo "── Е(ж) демон ДЕРЖИТ замок очереди до конца git commit ──"
# Поломка «проверил и отпустил» (ревью ЕЖ п. 1): демон видит замок свободным, а за
# ~70 с его pre-commit очередь берёт замок и сдвигает master посреди слияния. Хук
# стенда спит; пока он спит, flock -n пробы обязан получить отказ.
e_stand zh
printf '#!/usr/bin/env bash\n: > "%s/hook-started"\nsleep 4\n' "$EL" > "$EP/.git/hooks/pre-commit"
chmod +x "$EP/.git/hooks/pre-commit"
printf 'указание 1\n' >> "$EP/память/ORDERS.md"
e_progon "$EL/zh.out" &
E_DEMON=$!
for _ in $(seq 1 100); do [ -f "$EL/hook-started" ] && break; sleep 0.1; done
case_check "Е(ж) стенд: хук коммита запущен" "да" "$([ -f "$EL/hook-started" ] && echo да || echo нет)"
case_check "Е(ж) пока идёт pre-commit демона, замок очереди занят" "занят" \
    "$(flock -n "$ELOCK" true 2>/dev/null && echo свободен || echo занят)"
wait "$E_DEMON"
case_check "Е(ж) коммит прошёл" "да" "$(e_in_head память/ORDERS.md)"
case_check "Е(ж) после коммита замок отпущен" "свободен" \
    "$(flock -n "$ELOCK" true 2>/dev/null && echo свободен || echo занят)"

echo "── Е(з) потолок ожидания правки владельца MEMORY_OWNER_WAIT_MIN ──"
# Поломка (ревью ЕЖ п. 2): под долгой публикацией (36–76 мин держит замок) правка
# владельца ждёт без предела. Метка первого «отложил» — файл
# $LOG_DIR/memory-commit.отложено (mtime = время первого отказа); старше 30 мин —
# коммит владельца без оглядки на замок. Отметки одни потолка не получают.
e_stand z
printf 'указание 1\n' >> "$EP/память/ORDERS.md"
DO=$(e_head)
e_hold_lock
e_progon "$EL/z1.out"
case_check "Е(з) первый отказ: коммита нет" "$DO" "$(e_head)"
case_check "Е(з) метка первого «отложил» поставлена" "да" \
    "$([ -f "$EL/memory-commit.отложено" ] && echo да || echo нет)"
touch -d '-29 minutes' "$EL/memory-commit.отложено" 2>/dev/null
e_progon "$EL/z2.out"
case_check "Е(з) ждёт 29 мин — ещё не коммитит" "$DO" "$(e_head)"
touch -d '-31 minutes' "$EL/memory-commit.отложено" 2>/dev/null
e_progon "$EL/z3.out"
e_free_lock
# Новый коммит обязателен: ORDERS.md есть и в начальном коммите стенда, и без него
# e_in_head отвечал «да» на демоне, который так и не закоммитил.
case_check "Е(з) ждёт 31 мин — ORDERS.md закоммичен под занятым замком" "да" \
    "$([ "$(e_head)" != "$DO" ] && e_in_head память/ORDERS.md || echo нет)"
case_check "Е(з) коммит владельца мимо замка записан в журнал" "да" \
    "$(grep -q 'замок\|merge-queue' "$EL/z3.out" && echo да || echo нет)"
e_stand z2
printf 'отметка 1\n' >> "$EP/память/.tasks-triaged"
DO=$(e_head)
e_hold_lock
e_progon "$EL/z4.out"
touch -d '-31 minutes' "$EL/memory-commit.отложено" 2>/dev/null
e_progon "$EL/z5.out"
e_free_lock
case_check "Е(з) потолок — только правке владельца: отметки под замком ждут" "$DO" "$(e_head)"

printf '\nПАМЯТЬ-В-ГИТ: путей %d, неудач %d\n' "$CASES_PATHS" "$CASES_FAILED"
[ "$CASES_FAILED" -eq 0 ]
