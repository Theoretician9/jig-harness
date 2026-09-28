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
DEMON="$HERE/../harness/demons/memory-commit.sh"
paths=0
fails=0

itog() {  # $1=ждём $2=вышло $3=имя
    paths=$((paths + 1))
    if [ "$1" = "$2" ]; then printf '  ок    %s\n' "$3"
    else printf '  ПЛОХО %s: ждали «%s», вышло «%s»\n' "$3" "$1" "$2"; fails=$((fails + 1)); fi
}

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
cat > "$STAND/harness.conf" <<CONF
MEMORY_COMMIT_FAILS_MAX=3
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
        bash "$DEMON" >/dev/null 2>&1
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
itog "1" "$(skazano)" "владельцу сказано один раз"
itog "нет" "$(grep -q 'разберусь' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)" \
     "слова «разберусь» в сообщении нет"
itog "да" "$(grep -q 'повторю сам' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)" \
     "сказано, что демон повторит сам"
itog "да" "$(grep -q 'проба_гейта' "$STAND/zhurnal/kanal.txt" && echo да || echo нет)" \
     "красный гейт назван по имени"

echo "── тот же отказ на следующем обходе: владельцу не пишем ──"
progon
itog "1" "$(skazano)" "второго сообщения нет — обходы не спамят"

echo "── отказ держится дольше порога: долг на доску и одно слово владельцу ──"
progon
itog "2" "$(skazano)" "о затянувшемся отказе сказано ОДИН раз"
itog "да" "$(doska_est)" \
     "долг записан на доску — смена увидит его в стартовом отчёте"
progon
itog "2" "$(skazano)" "и на следующем обходе второй раз не повторяем"

echo "── гейт починился: долг снят, счёт обнулён ──"
zelenyj_gejt
progon
itog "2" "$(skazano)" "об успехе владельцу не пишем"
itog "нет" "$(doska_est)" \
     "запись о долге с доски снята"
itog "нет" "$([ -f "$STAND/zhurnal/pamyat-kommit.неудач" ] && echo да || echo нет)" \
     "счёт неудач подряд обнулён"
# quotepath=false: без него git печатает кириллицу escape-байтами, и проба
# искала бы слово, которого в выводе нет (память: quotepath).
itog "да" "$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%s | grep -q 'Правка памяти' && echo да || echo нет)" \
     "правка памяти в итоге закоммичена"

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
itog "да" "$NECHEGO" "вывод «nothing to commit» признан исчезнувшей правкой"
itog "нет" "$GEJT" "БОЛЬНОЙ СЛУЧАЙ: красный гейт исчезнувшей правкой НЕ считается"

echo "── БОЛЬНОЙ СЛУЧАЙ 28.09: НОВАЯ запись памяти обязана попасть в коммит ──"
# Коммит bf91834 назвал в сообщении MEMORY.md и вторую запись, а внёс один
# MEMORY.md: `git commit --only <путь>` берёт ТОЛЬКО отслеживаемые пути. Прежний
# стенд этого не видел — запись в нём была отслеженной «с самого начала», то
# есть слепое место было ВПИСАНО в пробу.
printf 'вторая редакция\n' > "$PROJECT/память/запись.md"
printf 'новая запись\n' > "$PROJECT/память/проба-новой-записи.md"
progon
SOSTAV="$(git -C "$PROJECT" -c core.quotepath=false show --name-only --format= HEAD 2>/dev/null || true)"
itog "да" "$(printf '%s\n' "$SOSTAV" | grep -q 'проба-новой-записи.md' && echo да || echo нет)" \
     "новая (неотслеженная) запись попала в коммит"
itog "да" "$(printf '%s\n' "$SOSTAV" | grep -q 'запись.md' && echo да || echo нет)" \
     "правка старой записи тоже в коммите"
itog "да" "$(git -C "$PROJECT" -c core.quotepath=false log -1 --format=%s \
             | grep -q 'проба-новой-записи.md' && echo да || echo нет)" \
     "сообщение коммита называет новую запись"
itog "" "$(git -C "$PROJECT" status --porcelain -- память 2>/dev/null || true)" \
     "после коммита в памяти не осталось неотслеженного"
# Сверка состава судится по СВОЕМУ следу: демон пишет в этот файл имена,
# которые назвал и не внёс. Первая редакция пробы искала в нём слово «БЕДА» —
# а оно уходит в вывод демона, не в файл: под мутацией проба оставалась
# зелёной и сверку не доказывала.
itog "0" "$(grep -c . "$STAND/zhurnal/pamyat-kommit.вывод.назвал-не-внёс" 2>/dev/null || true)" \
     "сверка состава: названных и не внесённых файлов нет"

printf '\nПАМЯТЬ-В-ГИТ: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
