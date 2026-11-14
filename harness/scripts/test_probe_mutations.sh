#!/usr/bin/env bash
#
# test_probe_mutations.sh — проверка ключа «--сверить-тексты» у мутационной пробы.
#
# Откуда взято. План docs/plans/2026-10-16-носители-живой-проверки.md, Task 4;
# спека живой проверки называет этот ключ в критериях Ф0 и Ф3, а на машине его
# не было вовсе.
#
# ЗАЧЕМ КЛЮЧ НУЖЕН. Мутация, чей текст «до» отстал от кода, НЕ краснеет и НЕ
# жалуется: прибор печатает «мутация не изменила файл» в журнал, который читают
# хвостом, а итог прогона выглядит точно как здоровое покрытие. После круга
# правок 13.10.2026 так отстали ДЕСЯТЬ мутаций из 78 — вызов ушёл на несколько
# строк, строка отказа переехала в свою функцию, литерал стал постоянной.
# Признак беды читается как «текст отстал от кода» — и как «мутация никого не
# покраснила», то есть ровно наоборот ([[a-mutation-text-left-behind-by-the-fix]]).
#
# ── Что охраняется сверх буквы плана ───────────────────────────────────────
# 1. **Текст, встречающийся ДВА раза, — тоже беда.** `sed` правит ПЕРВОЕ
#    вхождение, и мутация садится не туда, куда её писали: отступ делает
#    подстроку неоднозначной ([[a-mutation-that-reddens-nothing]]).
# 2. **Нераспознанное выражение обязано быть НАЗВАНО.** «Не разобрали» и
#    «текст на месте» — разные факты, и молчание о первом красит прибор
#    зелёным на непроверенном ([[unreadable-source-turns-check-green]]).
# 3. **Ноль судимых мутаций — отказ, а не успех.** Пустая таблица и сорванное
#    чтение данных выглядят одинаково; у соседнего прогона мутаций это правило
#    уже есть ([[a-gate-alive-with-empty-data]]).
# 4. **Каждый случай судит СВОЮ причину дословно.** Отказ по одному коду
#    возврата неотличим от отказа по другому.
#
# Каждый случай создаёт свой двор: свои носители и свой файл «механизма»,
# боевые носители сверяются по md5 до и после.
#
# Прогон: bash scripts/test_probe_mutations.sh
# Код возврата: 0 — все случаи зелёные, 1 — хоть один плохой.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
TOOL="scripts/mutation-probe.sh"
LIVE_INV="harness/config/invariants.yaml"
LIVE_PROBES="harness/config/probe-mutations.yaml"
GOOD=0; BAD=0
# Прогоны стенда игрушечные: дверь среза увела бы их в живую очередь flock к чужим
# прогонам, а из копии дерева — в скрипт, которого там нет.
export HARNESS_IN_CHECKS_SLICE=1
BEFORE=$(md5sum "$LIVE_INV" "$LIVE_PROBES" 2>/dev/null)

sluchaj() { # $1=имя $2=условие (0 — зелёный) $3=пояснение
    if [ "$2" = 0 ]; then
        printf '  ок    %s\n' "$1"; GOOD=$((GOOD+1))
    else
        printf '  ПЛОХО %s: %s\n' "$1" "$3"; BAD=$((BAD+1))
    fi
}

# Двор: каталог с «механизмом» и двумя носителями мутаций.
# $1 — выражение мутации для носителя проб, $2 — содержимое механизма.
dvor() {
    local yard; yard=$(mktemp -d "${TMPDIR:-/tmp}/мутации-проба-XXXXXX")
    mkdir -p "$yard"
    printf '%s\n' "$2" > "$yard/mehanizm.sh"
    cat > "$yard/invariants.yaml" <<YAML
инварианты: []
YAML
    cat > "$yard/probe-mutations.yaml" <<YAML
пробы:
  $yard/proba.sh:
    механизм: $yard/mehanizm.sh
    мутация: '$1'
    мутация_случай: 'мой случай'
    доказательство: bash $yard/proba.sh
    подмена: '-'
YAML
    printf '%s\n' "$yard"
}

# Вывод кладётся в ФАЙЛ, а код возврата — в переменную текущей оболочки.
# Первая редакция звала progon через $( … ): присваивание RC уходило в
# подоболочку, и `set -u` ронял пробу на «RC: unbound variable» — тот же класс,
# что чинили в самотесте мутационной пробы (cd без подоболочки).
progon() { # $1 = каталог двора; пишет вывод в $OUT, код в $RC
    local yard="$1"
    HARNESS_INVARIANTS="$yard/invariants.yaml" \
    HARNESS_PROBE_MUTATIONS="$yard/probe-mutations.yaml" \
    timeout 120 bash "$TOOL" --сверить-тексты > "$yard/vyvod.txt" 2>&1
    RC=$?
    OUT=$(cat "$yard/vyvod.txt")
}

# ── БОЛЬНОЙ СЛУЧАЙ ПЕРВЫМ: текст мутации отстал от кода ───────────────────
YARD=$(dvor 's/^PRIZNAK=1/PRIZNAK=0/' 'PRIZNAK=2')
progon "$YARD"
if [ "$RC" = 1 ] && printf '%s' "$OUT" | grep -q "НЕ НАЙДЕН" \
   && printf '%s' "$OUT" | grep -q "мой случай" \
   && printf '%s' "$OUT" | grep -q "не встало: 1"; then
    sluchaj "БОЛЬНОЙ СЛУЧАЙ: отставший текст назван, а не пропущен" 0 ""
else
    sluchaj "БОЛЬНОЙ СЛУЧАЙ: отставший текст назван, а не пропущен" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Текст встречается ДВА раза: sed сядет не туда ──────────────────────────
YARD=$(dvor 's/PRIZNAK=1/PRIZNAK=0/' 'PRIZNAK=1
  PRIZNAK=1')
progon "$YARD"
if [ "$RC" = 1 ] && printf '%s' "$OUT" | grep -q "встречается 2" \
   && printf '%s' "$OUT" | grep -q "не встало: 1"; then
    sluchaj "текст в двух местах назван неоднозначным" 0 ""
else
    sluchaj "текст в двух местах назван неоднозначным" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Нераспознанное выражение: не форма s/образец/замена/ ───────────────────
YARD=$(dvor '3d' 'PRIZNAK=1')
progon "$YARD"
if [ "$RC" = 1 ] && printf '%s' "$OUT" | grep -q "не форма s/" \
   && printf '%s' "$OUT" | grep -q "не встало: 1"; then
    sluchaj "нераспознанное выражение названо, а не принято" 0 ""
else
    sluchaj "нераспознанное выражение названо, а не принято" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Файла механизма нет: это НЕ «текст отстал» ─────────────────────────────
YARD=$(dvor 's/^PRIZNAK=1/PRIZNAK=0/' 'PRIZNAK=1')
rm -f "$YARD/mehanizm.sh"
progon "$YARD"
if [ "$RC" = 1 ] && printf '%s' "$OUT" | grep -q "файла нет"; then
    sluchaj "пропавший файл механизма назван своей причиной" 0 ""
else
    sluchaj "пропавший файл механизма назван своей причиной" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Пустая таблица: судить нечего — отказ, а не успех ──────────────────────
YARD=$(dvor 's/^PRIZNAK=1/PRIZNAK=0/' 'PRIZNAK=1')
cat > "$YARD/probe-mutations.yaml" <<'YAML'
пробы: {}
YAML
progon "$YARD"
if [ "$RC" = 1 ] && printf '%s' "$OUT" | grep -q "НИ ОДНОЙ мутации"; then
    sluchaj "пустая таблица — отказ со своей причиной" 0 ""
else
    sluchaj "пустая таблица — отказ со своей причиной" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Здоровый двор: текст на месте ровно один раз ───────────────────────────
YARD=$(dvor 's/^PRIZNAK=1/PRIZNAK=0/' 'PRIZNAK=1')
progon "$YARD"
if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q "не встало: 0"; then
    sluchaj "здоровый двор: не встало 0, код 0" 0 ""
else
    sluchaj "здоровый двор: не встало 0, код 0" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Выражение, которое sed НЕ ПРИНИМАЕТ ───────────────────────────────────
# Находка помощника 17.10.2026: разделитель внутри образца («s|a||b|c|») наш
# разбор принимал (брал усечённый образец), sed отказывался, файл не менялся —
# и беда читалась как «текст отстал от кода», то есть наоборот. Теперь
# грамматику судит САМ sed пробным прогоном, и слово печатает он.
YARD=$(dvor 's|^PRIZNAK=1||x|y|' 'PRIZNAK=1')
progon "$YARD"
if [ "$RC" = 1 ] && printf '%s' "$OUT" | grep -q "sed не принимает выражение" \
   && printf '%s' "$OUT" | grep -q "не встало: 1"; then
    sluchaj "БОЛЬНОЙ СЛУЧАЙ: выражение, которое sed не примет, названо своим словом" 0 ""
else
    sluchaj "БОЛЬНОЙ СЛУЧАЙ: выражение, которое sed не примет, названо своим словом" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Мутации ИНВАРИАНТОВ судятся тем же ключом ─────────────────────────────
YARD=$(dvor 's/^PRIZNAK=1/PRIZNAK=0/' 'PRIZNAK=1')
cat > "$YARD/invariants.yaml" <<YAML
инварианты:
  - id: и-проба
    файл: $YARD/mehanizm.sh
    доказательство: bash $YARD/mehanizm.sh --selftest
    мутация: 's/^НЕТ-ТАКОЙ-СТРОКИ/x/'
    мутация_случай: 'инвариантный случай'
YAML
progon "$YARD"
if [ "$RC" = 1 ] && printf '%s' "$OUT" | grep -q "инвариантный случай" \
   && printf '%s' "$OUT" | grep -q "НЕ НАЙДЕН"; then
    sluchaj "мутации инвариантов судятся тем же ключом" 0 ""
else
    sluchaj "мутации инвариантов судятся тем же ключом" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Ключ --механизм: гонится ОДНА запись, слепые соседи не выбраны ─────────
# Слепой механизм стоит и в инвариантах, и в пробах класса D: фильтр обязан
# снять обе таблицы, иначе дыра чужого механизма красит выбранный прогон.
YARD=$(dvor 's/^PRIZNAK=1/PRIZNAK=0/' 'PRIZNAK=1')
printf '#!/usr/bin/env bash\ngrep -q "^PRIZNAK=1" "$1"\n' > "$YARD/zrjachaja.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$YARD/slepaja.sh"
printf 'PRIZNAK=1\n' > "$YARD/slepoj.sh"
printf 'PRIZNAK=1\n' > "$YARD/slepoj2.sh"
cat > "$YARD/invariants.yaml" <<YAML
инварианты:
  - id: отбор-слепой-инвариант
    файл: $YARD/slepoj.sh
    доказательство: bash $YARD/slepaja.sh $YARD/slepoj.sh
    мутация: 's/^PRIZNAK=1/PRIZNAK=0/'
    мутация_случай: 'слепой инвариант'
YAML
cat > "$YARD/probe-mutations.yaml" <<YAML
пробы:
  $YARD/zrjachaja.sh:
    механизм: $YARD/mehanizm.sh
    мутация: 's/^PRIZNAK=1/PRIZNAK=0/'
    мутация_случай: 'зрячий класс D'
    доказательство: bash $YARD/zrjachaja.sh $YARD/mehanizm.sh
    подмена: '-'
  $YARD/slepaja.sh:
    механизм: $YARD/slepoj2.sh
    мутация: 's/^PRIZNAK=1/PRIZNAK=0/'
    мутация_случай: 'слепой класс D'
    доказательство: bash $YARD/slepaja.sh $YARD/slepoj2.sh
    подмена: '-'
YAML
# env -u MECH_FILTER: когда пробу зовёт сам прибор с --механизм, его фильтр уже
# экспортирован в окружение, и прибор без своего export читал бы ЧУЖОЙ фильтр.
env -u MECH_FILTER LOG_DIR="$YARD" HARNESS_INVARIANTS="$YARD/invariants.yaml" HARNESS_PROBE_MUTATIONS="$YARD/probe-mutations.yaml" \
    timeout 120 bash "$TOOL" --механизм "$YARD/mehanizm.sh" > "$YARD/vyvod.txt" 2>&1
RC=$?; OUT=$(cat "$YARD/vyvod.txt")
if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q "покраснели как надо 1, дыр 0" \
   && ! printf '%s' "$OUT" | grep -q "слепой"; then
    sluchaj "--механизм гоняет одну запись класса D, слепые соседи обеих таблиц не выбраны" 0 ""
else
    sluchaj "--механизм гоняет одну запись класса D, слепые соседи обеих таблиц не выбраны" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi
env -u MECH_FILTER LOG_DIR="$YARD" HARNESS_INVARIANTS="$YARD/invariants.yaml" HARNESS_PROBE_MUTATIONS="$YARD/probe-mutations.yaml" \
    timeout 120 bash "$TOOL" --механизм "$YARD/net-takogo.sh" > "$YARD/vyvod.txt" 2>&1
RC=$?; OUT=$(cat "$YARD/vyvod.txt")
if [ "$RC" = 2 ] && printf '%s' "$OUT" | grep -q "записей для механизма нет"; then
    sluchaj "--механизм без записей — код 2 и своё слово, не зелёный «0 из 0»" 0 ""
else
    sluchaj "--механизм без записей — код 2 и своё слово, не зелёный «0 из 0»" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── Кэш зелёных мутаций ────────────────────────────────────────────────────
# Мутация не гоняется заново, если механизм, проба и текст мутации те же, что
# в прошлом зелёном прогоне. Опасность кэша — проба «зелёная навсегда»: каждый
# случай ниже меняет РОВНО ОДНУ часть ключа и требует настоящего прогона.
# Признак настоящего прогона — строки, которые проба дописывает в zovy.txt при
# каждом зове (чистая копия + испорченная = 2), а не слово в выводе прибора.
kesh_dvor() { # $1 = тело пробы после записи о зове; печатает путь двора
    local yard; yard=$(mktemp -d "${TMPDIR:-/tmp}/мутации-кэш-XXXXXX")
    printf 'PRIZNAK=1\n' > "$yard/mehanizm.sh"
    printf '#!/usr/bin/env bash\necho zov >> "%s/zovy.txt"\n%s\n' "$yard" "$1" > "$yard/proba.sh"
    printf 'инварианты: []\n' > "$yard/invariants.yaml"
    kesh_tablica "$yard" 's/^PRIZNAK=1/PRIZNAK=0/'
    : > "$yard/zovy.txt"
    printf '%s\n' "$yard"
}
kesh_tablica() { # $1 = двор, $2 = выражение мутации
    cat > "$1/probe-mutations.yaml" <<YAML
пробы:
  $1/proba.sh:
    механизм: $1/mehanizm.sh
    мутация: '$2'
    мутация_случай: 'случай кэша'
    доказательство: bash $1/proba.sh $1/mehanizm.sh
    подмена: '-'
YAML
}
# Полный прогон таблицы двора (как его зовут ночные ворота); ZOVY — сколько раз
# проба позвана ЭТИМ прогоном. Срок кэша задан явно: проба не зависит от конфига.
kesh_progon() { # $1 = двор, $2 = срок кэша в сутках
    local yard="$1" before
    before=$(grep -c . "$yard/zovy.txt")
    env -u MECH_FILTER LOG_DIR="$yard" MUT_CACHE_DAYS="$2" \
        HARNESS_INVARIANTS="$yard/invariants.yaml" HARNESS_PROBE_MUTATIONS="$yard/probe-mutations.yaml" \
        timeout 120 bash "$TOOL" > "$yard/vyvod.txt" 2>&1
    RC=$?; OUT=$(cat "$yard/vyvod.txt")
    ZOVY=$(( $(grep -c . "$yard/zovy.txt") - before ))
}
kesh_sluchaj() { # $1 = имя, $2 = ждём зовов, $3 = ждём из кэша, $4 = ждём код
    if [ "$ZOVY" = "$2" ] && [ "$RC" = "$4" ] \
       && printf '%s' "$OUT" | grep -q "из кэша $3 ==="; then
        sluchaj "$1" 0 ""
    else
        sluchaj "$1" 1 "зовов пробы $ZOVY (ждали $2), код $RC (ждали $4), вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
    fi
}
ZRJACHAJA='grep -q "^PRIZNAK=1" "$1"'

# БОЛЬНОЙ СЛУЧАЙ ПЕРВЫМ: ничего не менялось — второй прогон пробу не зовёт.
YARD=$(kesh_dvor "$ZRJACHAJA")
kesh_progon "$YARD" 7
kesh_sluchaj "кэш: первый прогон зовёт пробу дважды (чистая и испорченная копии)" 2 0 0
kesh_progon "$YARD" 7
kesh_sluchaj "БОЛЬНОЙ СЛУЧАЙ: ничего не менялось — проба не позвана, итог из кэша" 0 1 0
if printf '%s' "$OUT" | grep -q "покраснели как надо 1, дыр 0" \
   && printf '%s' "$OUT" | grep -q "^  ок    из кэша.*случай кэша"; then
    sluchaj "кэш: попадание названо поимённо и засчитано в «покраснели»" 0 ""
else
    sluchaj "кэш: попадание названо поимённо и засчитано в «покраснели»" 1 "вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi
if [ -s "$YARD/mutations-cache.tsv" ]; then
    sluchaj "кэш лежит в LOG_DIR, а не в дереве" 0 ""
else
    sluchaj "кэш лежит в LOG_DIR, а не в дереве" 1 "нет $YARD/mutations-cache.tsv"
fi

# Изменилась ТОЛЬКО проба — механизм гоняется снова.
printf '# правка пробы\n' >> "$YARD/proba.sh"
kesh_progon "$YARD" 7
kesh_sluchaj "кэш: изменилась только ПРОБА — мутация прогнана заново" 2 0 0

# Изменился ТОЛЬКО механизм — тоже заново; следующий прогон снова из кэша.
printf '# правка механизма\n' >> "$YARD/mehanizm.sh"
kesh_progon "$YARD" 7
kesh_sluchaj "кэш: изменился только МЕХАНИЗМ — мутация прогнана заново" 2 0 0
kesh_progon "$YARD" 7
kesh_sluchaj "кэш: после нового зелёного прогона снова из кэша" 0 1 0

# Изменился ТОЛЬКО текст мутации — это другая мутация.
kesh_tablica "$YARD" 's/^PRIZNAK=1/PRIZNAK=2/'
kesh_progon "$YARD" 7
kesh_sluchaj "кэш: изменился только ТЕКСТ мутации — прогнана заново" 2 0 0

# Срок: запись старше MUT_CACHE_DAYS не засчитывается — раз в N суток каждая
# мутация гоняется по-настоящему, это страховка от соседей вне ключа.
awk -F'\t' -v OFS='\t' '{ $2 = $2 - 8*86400; print }' "$YARD/mutations-cache.tsv" > "$YARD/kesh.tmp" \
    && mv "$YARD/kesh.tmp" "$YARD/mutations-cache.tsv"
kesh_progon "$YARD" 7
kesh_sluchaj "кэш: запись старше срока (8 суток при сроке 7) — прогнана заново" 2 0 0
kesh_progon "$YARD" 0
kesh_sluchaj "кэш: срок 0 — кэш выключен, прогнана заново" 2 0 0

# Запись ИНВАРИАНТА с дверью среды: путь механизма приходит переменной MEH и
# в команде его нет, а путь пробы стоит только в команде. Здесь у каждого файла
# одна дорога в ключ — у записи класса D выше обе стоят и в команде, и в полях,
# и выпадение одной дороги там не видно.
YARD=$(kesh_dvor 'grep -q "^PRIZNAK=1" "$MEH"')
printf 'пробы: {}\n' > "$YARD/probe-mutations.yaml"
cat > "$YARD/invariants.yaml" <<YAML
инварианты:
  - id: кэш-инвариант
    файл: $YARD/mehanizm.sh
    доказательство: bash $YARD/proba.sh
    мутация: 's/^PRIZNAK=1/PRIZNAK=0/'
    мутация_случай: 'случай кэша'
    мутация_подмена: MEH
YAML
kesh_progon "$YARD" 7
kesh_progon "$YARD" 7
kesh_sluchaj "кэш инварианта: ничего не менялось — из кэша" 0 1 0
printf '# правка пробы\n' >> "$YARD/proba.sh"
kesh_progon "$YARD" 7
kesh_sluchaj "кэш инварианта: изменилась только проба (путь лишь в команде) — заново" 2 0 0
printf '# правка механизма\n' >> "$YARD/mehanizm.sh"
kesh_progon "$YARD" 7
kesh_sluchaj "кэш инварианта: изменился только механизм (пути в команде нет) — заново" 2 0 0

# Дыра кэшируется, но вердикт остаётся красным: слепую пробу второй раз не
# зовут (ночь тратила ~6 ч на повтор известных дыр), а итог по-прежнему её
# считает. Опасность здесь обратная зелёному кэшу — известная дыра, тихо
# ставшая «ок» или пропавшая из счёта.
YARD=$(kesh_dvor 'exit 0')
kesh_progon "$YARD" 7
kesh_progon "$YARD" 7
if [ "$ZOVY" = 0 ] && [ "$RC" = 1 ] && printf '%s' "$OUT" | grep -q "дыр 1, из кэша 1" \
   && printf '%s' "$OUT" | grep -q "^  ПЛОХО (известная, из кэша.*случай кэша"; then
    sluchaj "кэш: дыра при втором прогоне берётся из кэша и вердикт красный" 0 ""
else
    sluchaj "кэш: дыра при втором прогоне берётся из кэша и вердикт красный" 1 "зовов $ZOVY, код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# Строки кэша до поля «дыра» (три поля) — зелёные попадания, а не дыры.
YARD=$(kesh_dvor "$ZRJACHAJA")
kesh_progon "$YARD" 7
cut -f1-3 "$YARD/mutations-cache.tsv" > "$YARD/kesh.tmp" && mv "$YARD/kesh.tmp" "$YARD/mutations-cache.tsv"
kesh_progon "$YARD" 7
kesh_sluchaj "кэш: старая строка из трёх полей — зелёное попадание" 0 1 0

# ── Выборка записей: проба из scripts/ читает harness/config ──────────────
# БОЛЬНОЙ СЛУЧАЙ 29.10.2026 (находка P2): прогон по ВЫБОРКЕ
# (свои HARNESS_INVARIANTS и HARNESS_PROBE_MUTATIONS) собирал копию дерева из
# корней одних выбранных записей. У записи с механизмом и пробой в scripts/ это
# один scripts/, а проба читает harness/config/… — чистая копия краснела, и
# мутация шла в «дыры» без вины пробы. В полной таблице спасал порядок записей:
# чужая запись раньше уже скопировала harness/.
#
# КОНТРАКТ ДЛЯ ПРИБОРА: в выборке из одной записи класса D (механизм и проба в
# scripts/, подмена «из-копии», проба читает ../harness/config рядом с собой)
# прибор отвечает кодом 0 и «покраснели как надо 1, дыр 0», без «чистая копия
# уже красная». Стенд — своё дерево с копией прибора: прибор судит дерево, в
# котором лежит сам. Дверь HARNESS_TEST_MUTPROBE_TOOL подставляет копию прибора
# для проверки живости этого случая.
ROOTS=$(mktemp -d "${TMPDIR:-/tmp}/мутации-выборка-XXXXXX")
mkdir -p "$ROOTS/scripts/lib" "$ROOTS/harness/config"
cp "${HARNESS_TEST_MUTPROBE_TOOL:-$TOOL}" "$ROOTS/scripts/mutation-probe.sh"
cp scripts/lib/config.sh "$ROOTS/scripts/lib/config.sh"
printf 'ZNACH=1\n' > "$ROOTS/harness/config/flag.conf"
printf 'PRIZNAK=1\n' > "$ROOTS/scripts/mehanizm.sh"
cat > "$ROOTS/scripts/proba.sh" <<'PROBA'
#!/usr/bin/env bash
here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
. "$here/../harness/config/flag.conf" || exit 1
. "$here/mehanizm.sh"
[ "${ZNACH:-}" = 1 ] && [ "${PRIZNAK:-}" = 1 ]
PROBA
printf 'инварианты: []\n' > "$ROOTS/invariants.yaml"
cat > "$ROOTS/probe-mutations.yaml" <<'YAML'
пробы:
  scripts/proba.sh:
    механизм: scripts/mehanizm.sh
    мутация: 's/^PRIZNAK=1/PRIZNAK=0/'
    мутация_случай: 'проба читает harness/config'
    доказательство: bash scripts/proba.sh
    подмена: из-копии
YAML
env -u MECH_FILTER LOG_DIR="$ROOTS" MUT_CACHE_DAYS=0 \
    HARNESS_INVARIANTS="$ROOTS/invariants.yaml" HARNESS_PROBE_MUTATIONS="$ROOTS/probe-mutations.yaml" \
    timeout 120 bash "$ROOTS/scripts/mutation-probe.sh" > "$ROOTS/vyvod.txt" 2>&1
RC=$?; OUT=$(cat "$ROOTS/vyvod.txt")
if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q "покраснели как надо 1, дыр 0" \
   && ! printf '%s' "$OUT" | grep -q "чистая копия уже красная"; then
    sluchaj "БОЛЬНОЙ СЛУЧАЙ: выборка из одной записи — копия несёт harness/, чистая зелёная, мутация поймана" 0 ""
else
    sluchaj "БОЛЬНОЙ СЛУЧАЙ: выборка из одной записи — копия несёт harness/, чистая зелёная, мутация поймана" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

# ── БОЛЬНОЙ СЛУЧАЙ 07.11.2026: ключ «проба#суффикс», проба читает карту проекта ──
# test_events_cli.py#lib (Ф1б-1…6): суффикс записи уходил путём пробы — «мутацию
# некуда подставить»; после этого чистая копия краснела: проба читает
# ROOT/dev-map.yaml, а в копию шли только scripts/ и harness/.
printf 'KARTA: 1\n' > "$ROOTS/dev-map.yaml"
printf 'PRIZNAK=1\n' > "$ROOTS/scripts/mehanizm_karty.sh"
cat > "$ROOTS/scripts/proba_karty.sh" <<'PROBA'
#!/usr/bin/env bash
here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
grep -q '^KARTA: 1$' "$here/../dev-map.yaml" || exit 1
. "$here/mehanizm_karty.sh"
[ "${PRIZNAK:-}" = 1 ]
PROBA
cat > "$ROOTS/probe-mutations.yaml" <<'YAML'
пробы:
  scripts/proba_karty.sh#suffiks:
    механизм: scripts/mehanizm_karty.sh
    мутация: 's/^PRIZNAK=1/PRIZNAK=0/'
    мутация_случай: 'проба с суффиксом читает карту'
    доказательство: bash scripts/proba_karty.sh
    подмена: из-копии
YAML
env -u MECH_FILTER LOG_DIR="$ROOTS" MUT_CACHE_DAYS=0 \
    HARNESS_INVARIANTS="$ROOTS/invariants.yaml" HARNESS_PROBE_MUTATIONS="$ROOTS/probe-mutations.yaml" \
    timeout 120 bash "$ROOTS/scripts/mutation-probe.sh" --механизм scripts/mehanizm_karty.sh > "$ROOTS/vyvod.txt" 2>&1
RC=$?; OUT=$(cat "$ROOTS/vyvod.txt")
if [ "$RC" = 0 ] && printf '%s' "$OUT" | grep -q "покраснели как надо 1, дыр 0"; then
    sluchaj "БОЛЬНОЙ СЛУЧАЙ: запись «проба#суффикс» из копии, карта проекта в копии — мутация поймана" 0 ""
else
    sluchaj "БОЛЬНОЙ СЛУЧАЙ: запись «проба#суффикс» из копии, карта проекта в копии — мутация поймана" 1 "код $RC, вывод: $(printf '%s' "$OUT" | tr '\n' '|' | cut -c1-300)"
fi

AFTER=$(md5sum "$LIVE_INV" "$LIVE_PROBES" 2>/dev/null)
if [ "$BEFORE" = "$AFTER" ]; then
    sluchaj "боевые носители не тронуты" 0 ""
else
    sluchaj "боевые носители не тронуты" 1 "проба сыграла на производстве"
fi

printf 'ИТОГ: хороших %s, плохих %s\n' "$GOOD" "$BAD"
[ "$BAD" = 0 ]
