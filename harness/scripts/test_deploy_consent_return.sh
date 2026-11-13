#!/usr/bin/env bash
# Проба: выкат, упавший ПОСЛЕ ворот согласия, кнопку «Да» владельца не тратит.
#
# Откуда взята. Прежде разрешение расходовалось ПРОХОДОМ ворот, а выкат шёл
# дальше: секреты, сборка, health, смоук. Упади он там — «да» владельца
# потрачено на действие, которого не было (у выката дефект жил до 28.09.2026).
# С 06.11.2026 ворота кнопку не тратят вовсе: гасит её только начатый выкат —
# строка журнала выкатов deploy-releases.jsonl (owner_word.ПОГАШЕНИЕ). Проба
# судит, что отказ на секретах эту строку не пишет и кнопка годна повтору.
#
# Проверка СОЗДАЁТ своё условие: свой паспорт, свой журнал, свой проект с
# подставным секретом, своя копия scripts/ с обёрткой ворот (носитель стенда) —
# без неё ворота судили бы журнал и ключи согласия машины, а не стенда. Секрет
# ГЕНЕРИРУЕТСЯ, а не берётся с машины (память: секрет-пробы-был-настоящим).
#
# Имена переменных ЛАТИНИЦЕЙ: bash не берёт кириллические идентификаторы.
set -uo pipefail
export LC_ALL=C.UTF-8

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
mkdir -p "$PROJECT" "$STAND/zhurnal" "$STAND/sekrety"
cat > "$STAND/install.conf" <<CONF
PROJECT_NAME="proba"
PROJECT_DIR="$PROJECT"
LOG_DIR="$STAND/zhurnal"
SECRETS_DIR="$STAND/sekrety"
TMUX_SESSION="net-takoj"
AGENT_START_CMD="true"
AUTONOMY="semi"
HEARTBEAT_DIR="$STAND/zhurnal/hb"
TG_CHAT_ID="4242"
CONF
# Продукт стенда ОБЪЯВЛЕН: без этого выкат честно выходит нулём на шаге 0
# («продукт не задан»), ворота согласия даже не зовутся — и проба зеленела бы,
# не дойдя до проверяемого места. Сборка стенда — `true`: до неё выкат не
# доходит, его останавливает гейт секретов. Ключи согласия — строгие: выкат
# под кнопкой, стоящего согласия нет.
printf 'STACK_IMAGES=""\nSTACK_BUILD_CMD="true"\nSTACK_COMPOSE=""\nDEPLOY_NEEDS_OWNER=1\nDEPLOY_CONSENT_STANDING=0\nCONFIRM_MAX_AGE_MIN=60\nCONSENT_BUTTON_TTL_MIN=15\n' \
    > "$STAND/harness.conf"
: > "$STAND/zhurnal/inbox.jsonl"
# Дверь (АР-3) HARNESS_INSTALL_CONF/HARNESS_CONF не слушает: паспорт стенда для КОПИИ двери
# вне PROJECT_DIR машины называет HARNESS_DOOR_STAND_HOME; остальные — для детей двери.
export HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
       HARNESS_LOG_DIR="$STAND/zhurnal" HARNESS_DOOR_STAND_HOME="$STAND"

# Обёртка ворот пробы: настоящие ворота deploy_guard.py в процессе обёртки, их судье —
# носитель стенда. После карточки А судья не слушает окружение ни при каком ключе
# (спека §3.2), и отдельным процессом дверь «Да» стенда не видит — открытие проба
# судит только так (отступление 7 плана), отказ — и отдельным процессом.
# Без Носитель у судьи (до А3) обёртка печатает «ОБЁРТКА ПРОБЫ: …» и выходит кодом 3.
obertka_vorot() {  # $1=файл обёртки $2=настоящие ворота $3=паспорт стенда $4=ручки стенда
    # Путь обёртки бывает ссылкой на настоящие ворота (проект стенда из ссылок): открыть его
    # на запись значило бы переписать сами ворота, поэтому ссылка снимается до записи.
    rm -f "$1"
    python3 - "$1" "$(readlink -f "$2")" "$3" "$4" <<'PY'
import sys
out, real, passport, handles = sys.argv[1:5]
head = f"REAL, PASSPORT, HANDLES = {real!r}, {passport!r}, {handles!r}\n"
body = r'''
import dataclasses, importlib.util, pathlib, sys

def number(value, default):
    text = str(value or "").strip()
    return int(text) if text.isdigit() else default

def stand_carrier(judge, config):
    cls = getattr(judge, "Носитель", None)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise LookupError("в судье нет dataclass Носитель")
    values, keys = config.из_файла(PASSPORT), config.из_файла(HANDLES)
    folder = pathlib.Path(values.get("LOG_DIR") or PASSPORT)
    known = {"папка": folder, "uid": None, "паспорт": values, "граница": folder,
             "окно_мин": number(keys.get("CONFIRM_MAX_AGE_MIN"), 60),
             "срок_кнопки_мин": number(keys.get("CONSENT_BUTTON_TTL_MIN"), None), "ручки": keys}
    fields = dataclasses.fields(cls)
    unknown = [f.name for f in fields if f.name not in known
               and f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING]
    if unknown:
        raise LookupError(f"у Носитель обязательные поля, которых проба не знает: {unknown}")
    return cls(**{f.name: known[f.name] for f in fields if f.name in known})

sys.argv[0] = REAL
spec = importlib.util.spec_from_file_location("deploy_guard", REAL)
door = importlib.util.module_from_spec(spec)
spec.loader.exec_module(door)
try:
    carrier = stand_carrier(door.слово_вл, door.конф)
except LookupError as trouble:
    print(f"ОБЁРТКА ПРОБЫ: {trouble}")
    sys.exit(3)
door.слово_вл.носитель = lambda: carrier
sys.exit(door.main())
'''
with open(out, "w", encoding="utf-8") as fh:
    fh.write("# Обёртка пробы (носитель стенда): сгенерирована пробой, не править.\n" + head + body)
PY
}

# Копия scripts/: deploy.sh зовёт ворота рядом с собой ($SCRIPT_DIR по readlink -f),
# поэтому настоящими копиями лежат он, сверка секретов и lib, а ворота — обёрткой.
KOPIYA="$STAND/scripts"
mkdir -p "$KOPIYA"
cp "$HERE/deploy.sh" "$HERE/check-secrets.sh" "$KOPIYA/"
cp -r "$HERE/lib" "$KOPIYA/"
obertka_vorot "$KOPIYA/deploy_guard.py" "$HERE/deploy_guard.py" "$STAND/install.conf" "$STAND/harness.conf"

# Подставной секрет собирается из кусков: цельная строка в файле пробы сама
# выглядела бы утечкой для гейта секретов.
TOKEN_CHISLA="$(head -c 9 /dev/urandom | od -An -tu1 | tr -d ' \n' | cut -c1-9)"
TOKEN_BUKVY="$(head -c 32 /dev/urandom | base64 | tr -d '+/=' | cut -c1-35)"
printf 'TOKEN = "%s:%s"\n' "$TOKEN_CHISLA" "$TOKEN_BUKVY" > "$PROJECT/config.py"
git -C "$PROJECT" init -q
git -C "$PROJECT" -c user.email="proba@stend" -c user.name="проба" add -A
git -C "$PROJECT" -c user.email="proba@stend" -c user.name="проба" commit -qm "стенд"
# Предмет выката считает проба сама: «<sha> <realpath дерева>».
PREDMET="$(git -C "$PROJECT" rev-parse HEAD) $(readlink -f "$PROJECT")"

knopka_da() {  # «Да» кнопкой на предмет выката — конверт, как его пишет диспетчер канала
    python3 - "$STAND/zhurnal" "$PREDMET" <<'PY'
import datetime, json, sys
log, target = sys.argv[1], sys.argv[2]
ts = (datetime.datetime.now().astimezone() - datetime.timedelta(minutes=1)).isoformat(timespec="seconds")
data = {"nonce": "proba-vozvrat", "вид": "выкат", "цель": target, "message_id": 4000, "from_id": "4242"}
# Живой порядок: просьба ask-consent не позже нажатия, нажатие — с from_id владельца стенда.
with open(f"{log}/consent-requests.jsonl", "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"v": 1, "ts": ts, "src": "scripts/ask-consent.py", "event": "consent" + ".request",
                         "outcome": "ok", "data": {"nonce": "proba-vozvrat", "вид": "выкат", "цель": target,
                                                  "почему": "проба"}}, ensure_ascii=False) + "\n")
with open(f"{log}/consent-buttons.jsonl", "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"v": 1, "ts": ts, "src": "проба", "event": "consent" + ".yes", "outcome": "ok",
                         "data": data}, ensure_ascii=False) + "\n")
PY
}

knopka_v_sile() {  # «да» — ворота пускают по кнопке, «нет» — не пускают, «обёртка» — судить нечем
    local otvet rc=0
    otvet=$(python3 "$KOPIYA/deploy_guard.py" --действие deploy --только-согласие 2>&1) || rc=$?
    if printf '%s' "$otvet" | grep -q "ОБЁРТКА ПРОБЫ"; then echo обёртка
    elif [ "$rc" = 0 ] && printf '%s' "$otvet" | grep -q "согласие кнопкой"; then echo да
    else echo нет; fi
}

echo "── контроль стенда: без кнопки ворота не пускают ──"
itog "нет" "$(knopka_v_sile)" "стенд строгий: ключи стенда, кнопки нет — закрыто"

echo "── БОЛЬНОЙ СЛУЧАЙ: выкат упал ПОСЛЕ ворот — кнопка «Да» не сгорает ──"
knopka_da
itog "да" "$(knopka_v_sile)" "стенд начинает с «Да» кнопкой на предмет выката"

VYVOD=$(timeout 120 bash "$KOPIYA/deploy.sh" 2>&1)
RC=$?
printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -8
itog "да" "$(printf '%s' "$VYVOD" | grep -q "согласие кнопкой" && echo да || echo нет)" \
     "выкат прошёл ворота ПО КНОПКЕ, а не вышел раньше и не по ключу машины"
itog "1" "$RC" "выкат отказал на секретах в проекте стенда"
itog "да" "$(printf '%s' "$VYVOD" | grep -q "секрет" && echo да || echo нет)" \
     "причина отказа названа словом «секрет»"
# Судим НОСИТЕЛЬ погашения, а не только повторный вопрос: строка журнала выкатов с
# этим предметом погасила бы «Да» — отказ на секретах её писать не вправе.
itog "0" "$(cat "$STAND/zhurnal/deploy-releases.jsonl" 2>/dev/null | grep -c -F "$PREDMET")" \
     "БОЛЬНОЙ СЛУЧАЙ: отказ на секретах не записал выкат — кнопка не погашена"
itog "да" "$(knopka_v_sile)" "после отказа та же кнопка по-прежнему пускает"
itog "нет" "$( [ -e "$STAND/zhurnal/согласие.json" ] && echo да || echo нет)" \
     "ворота и выкат не пишут согласие.json — тратить нечего"

printf '\nВОЗВРАТ-СОГЛАСИЯ: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
