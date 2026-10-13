#!/bin/bash
# Проба: диспетчер будит смену, когда работы у неё нет.
#
# Больной случай 28.09.2026. В 17:30:47 владелец снял паузу, в 17:31:49
# прислал новую задачу — диспетчер записал «несрочное: шаг агента не
# прерываю» и пинка не дал. Шага не было: смена стояла без работы с 14:26.
# Владелец ждал четыре минуты и переспросил, дошло ли сообщение. Беречь
# занятость, не проверяя её, — то же самое, что не беречь ничего.
set -u
KOREN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
ISHOD=0
STEND=$(mktemp -d)
KOPIYA="$KOREN/scripts/test_dispatcher_busy_copy.sh"
trap 'rm -rf "$STEND"; rm -f "$KOPIYA"' EXIT

# Функция берётся из БОЕВОГО файла копией рядом с ним: переписанная здесь,
# она сравнивала бы себя с собой ([[comparing-two-implementations-turns-tautological]]).
cp "$KOREN/scripts/tg-dispatcher.sh" "$KOPIYA"

proverit() {  # $1 — PROJECT_DIR стенда, $2 — ждём, $3 — о чём проба
    local otvet
    # HARNESS_ACTIVE_LINE="" — «над всем»: иначе прибор отбирает задачи по
    # ЖИВОЙ настройке направления, и стенд пробы зависел бы от неё.
    otvet=$(PROJECT_DIR="$1" HERE="$KOREN/scripts" HARNESS_ACTIVE_LINE="" bash -c '
        source <(sed -n "/^smena_zanyata()/,/^}/p" '"$KOPIYA"')
        smena_zanyata && echo занята || echo свободна')
    if [ "$otvet" = "$2" ]; then
        echo "  ок    $3"
    else
        echo "  ПЛОХО $3: ждали $2, вышло $otvet"
        ISHOD=1
    fi
}

# Обе карты — СВОИ. Прежде второй путь судил БОЕВУЮ карту проекта, то есть
# совпадал с машиной вместо того, чтобы создать себе условие: карта с законно
# пустой очередью красила пробу, и 04.10.2026 она была красной на коде HEAD
# ([[a-check-creates-its-own-condition]]).
mkdir -p "$STEND/net-raboty" "$STEND/est-rabota"
printf 'queue: []\nlines: []\n' > "$STEND/net-raboty/dev-map.yaml"
printf 'queue: [pr.next]\nlines:\n  - tasks:\n      - id: pr.next\n' > "$STEND/est-rabota/dev-map.yaml"
proverit "$STEND/net-raboty" свободна "БОЛЬНОЙ СЛУЧАЙ: работы нет — смену будим, беречь нечего"
proverit "$STEND/est-rabota" занята "на карте есть работа — шаг смены берегут"
proverit "$STEND/net-takogo" занята "карты нет — считаем занятой: молчаливый пинок хуже ожидания"

[ "$ISHOD" = 0 ] && echo "ПРОБА ПРОЙДЕНА: 3 пути, первый — больной случай"
exit "$ISHOD"
