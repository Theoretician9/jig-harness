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
# подставным секретом, ключ стенда HARNESS_OWNER_WORD_STEND=1 — без него ворота
# читали бы ключи согласия у машины, а не у стенда. Секрет ГЕНЕРИРУЕТСЯ, а не
# берётся с машины (память: секрет-пробы-был-настоящим).
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
export HARNESS_OWNER_WORD_STEND=1 HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
       HARNESS_LOG_DIR="$STAND/zhurnal"

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

knopka_v_sile() {  # «да» — ворота пускают по кнопке, «нет» — не пускают
    local otvet
    otvet=$(python3 "$HERE/deploy_guard.py" --действие deploy --только-согласие 2>&1) \
        && printf '%s' "$otvet" | grep -q "согласие кнопкой" && echo да || echo нет
}

echo "── контроль стенда: без кнопки ворота не пускают ──"
itog "нет" "$(knopka_v_sile)" "стенд строгий: ключи стенда, кнопки нет — закрыто"

echo "── БОЛЬНОЙ СЛУЧАЙ: выкат упал ПОСЛЕ ворот — кнопка «Да» не сгорает ──"
knopka_da
itog "да" "$(knopka_v_sile)" "стенд начинает с «Да» кнопкой на предмет выката"

VYVOD=$(timeout 120 bash "$HERE/deploy.sh" 2>&1)
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
