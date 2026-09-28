#!/usr/bin/env bash
# Проба: отказ ПОСЛЕ ворот согласия оставляет разрешение владельца в силе.
#
# Откуда взята. Разрешение расходуется ПРОХОДОМ ворот, а выкат идёт дальше:
# секреты, сборка, health, смоук. Упади он там — «да» владельца потрачено на
# действие, которого не было, и просить приходится второй раз. У публикации это
# чинилось 12.09.2026 (упала через две строки после замка), у выката тот же
# дефект жил до 28.09.2026.
#
# Проверка СОЗДАЁТ своё условие: свой паспорт, свой журнал согласий, свой
# проект с подставным секретом — боевую установку не трогаем. Секрет
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
CONF
# Продукт стенда ОБЪЯВЛЕН: без этого выкат честно выходит нулём на шаге 0
# («продукт не задан»), ворота согласия даже не зовутся — и проба зеленела бы,
# не дойдя до проверяемого места. Сборка стенда — `true`: до неё выкат не
# доходит, его останавливает гейт секретов двумя шагами раньше.
printf 'STACK_IMAGES=""\nSTACK_BUILD_CMD="true"\nSTACK_COMPOSE=""\n' > "$STAND/harness.conf"

# Подставной секрет собирается из кусков: цельная строка в файле пробы сама
# выглядела бы утечкой для гейта секретов.
TOKEN_CHISLA="$(head -c 9 /dev/urandom | od -An -tu1 | tr -d ' \n' | cut -c1-9)"
TOKEN_BUKVY="$(head -c 32 /dev/urandom | base64 | tr -d '+/=' | cut -c1-35)"
printf 'TOKEN = "%s:%s"\n' "$TOKEN_CHISLA" "$TOKEN_BUKVY" > "$PROJECT/config.py"
git -C "$PROJECT" init -q
git -C "$PROJECT" -c user.email="proba@stend" -c user.name="проба" add -A
git -C "$PROJECT" -c user.email="proba@stend" -c user.name="проба" commit -qm "стенд"

svezhee_da() {  # свежее «да» владельца и ожидание именно на выкат
    python3 - "$STAND" <<'PY'
import json, sys, time
stand = sys.argv[1]
ts = time.strftime("%Y-%m-%dT%H:%M:%S+05:00")
with open(f"{stand}/zhurnal/confirmations.jsonl", "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"ts": ts, "text": "да", "message_id": "1",
                         "kind": "any"}, ensure_ascii=False) + "\n")
with open(f"{stand}/zhurnal/согласие.json", "w", encoding="utf-8") as fh:
    json.dump({"ожидание": {"действие": "deploy", "ts": time.time()}}, fh,
              ensure_ascii=False)
PY
}

soglasie_v_sile() {  # «да» — разрешение ещё действует, «нет» — потрачено
    local otvet
    otvet=$(HARNESS_INSTALL_CONF="$STAND/install.conf" \
            HARNESS_CONF="$STAND/harness.conf" \
            python3 "$HERE/deploy_guard.py" --действие deploy \
                    --только-согласие --не-тратить 2>&1 || true)
    case "$otvet" in
        *"нет согласия"*) echo нет ;;
        *)                echo да ;;
    esac
}

echo "── БОЛЬНОЙ СЛУЧАЙ: выкат упал ПОСЛЕ ворот — «да» владельца не сгорает ──"
svezhee_da
itog "да" "$(soglasie_v_sile)" "стенд начинает со свежим согласием"

VYVOD=$(HARNESS_INSTALL_CONF="$STAND/install.conf" HARNESS_CONF="$STAND/harness.conf" \
        timeout 120 bash "$HERE/deploy.sh" 2>&1)
RC=$?
printf '%s\n' "$VYVOD" | sed 's/^/      /' | tail -8
itog "да" "$(printf '%s' "$VYVOD" | grep -q "согласие владельца" && echo да || echo нет)" \
     "выкат дошёл ДО ворот согласия, а не вышел на шаге «продукта нет»"
itog "1" "$RC" "выкат отказал на секретах в проекте стенда"
itog "да" "$(printf '%s' "$VYVOD" | grep -q "секрет" && echo да || echo нет)" \
     "причина отказа названа словом «секрет»"
itog "да" "$(soglasie_v_sile)" \
     "после отказа ворота по-прежнему пускают (окно повторной попытки)"
# Судим СОСТОЯНИЕ, а не повторный вопрос: повтор того же действия попадает в
# окно «та же попытка» и зеленеет даже у ПОТРАЧЕННОГО разрешения — без этого
# пути проба проходила и с убранной ловушкой возврата (мутация 28.09.2026).
IZRASHODOVANO=$(python3 -c 'import json,sys
try:
    s = json.load(open(sys.argv[1] + "/zhurnal/согласие.json", encoding="utf-8"))
except Exception:
    print("нечитаемо"); raise SystemExit
print(len(s.get("израсходованы") or []))' "$STAND")
itog "0" "$IZRASHODOVANO" \
     "БОЛЬНОЙ СЛУЧАЙ: разрешение НЕ числится израсходованным — выкат его вернул"

printf '\nВОЗВРАТ-СОГЛАСИЯ: путей %d, неудач %d\n' "$paths" "$fails"
[ "$fails" -eq 0 ]
