#!/usr/bin/env bash
# Проба: шаблоны юнитов в USTANOVIT.sh (sentinel-boot и dispatcher) не расходятся с живыми.
#
# Откуда взята. 03.11.2026 после перезагрузки harness-sentinel-boot отработал раньше стены
# и docker и дал ложное «НЕ ПОДНЯЛСЯ»: в After= не было юнитов, которые проверяет
# boot-check.yaml. И в шаблоне dispatcher не было PANEL_SECRETS_DIR, которое есть в живом юните.
# Блок шаблона достаётся ПАРСИНГОМ установщика (от записать_файл до EOF); пустой блок — провал.
# Дверь мутаций: USTANOVIT_UNDER_TEST — установщик, который судит проба.
set -u
here=$(cd "$(dirname "$0")/.." && pwd)
src=${USTANOVIT_UNDER_TEST:-$here/../USTANOVIT.sh}
# В развёрнутом проекте установщика рядом нет: проба судит исходник пакета, и там
# молчаливый зелёный хуже жёлтого (77 — «не смогла проверить»).
[ -f "$src" ] || { echo "SKIP: установщик $src не найден (проба живёт в пакете)"; exit 77; }
fail=0
block() {
  awk -v unit="$1" '
    index($0, "записать_файл /etc/systemd/system/" unit ".service") {on=1; next}
    on && /^EOF$/ {exit}
    on {print}' "$src"
}
check() { # блок, шаблон, описание
  if grep -Eq -- "$2" <<<"$1"; then echo "ok:   $3"; else echo "FAIL: $3"; fail=1; fi
}
boot=$(block harness-sentinel-boot)
disp=$(block harness-dispatcher)
[ -n "$boot" ] || { echo "FAIL: блок harness-sentinel-boot не найден"; exit 1; }
[ -n "$disp" ] || { echo "FAIL: блок harness-dispatcher не найден"; exit 1; }
for u in docker nginx php8.3-fpm harness-panel harness-firewall; do
  check "$(grep '^After=' <<<"$boot")" "[ =]$u\.service( |$)" "sentinel-boot: After= содержит $u"
done
for u in docker nginx php8.3-fpm harness-panel harness-firewall; do
  check "$(grep '^Wants=' <<<"$boot")" "[ =]$u\.service( |$)" "sentinel-boot: $u желаемый (Wants)"
done
if grep -q '^Requires=' <<<"$boot"; then echo "FAIL: sentinel-boot: Requires= запрещён"; fail=1; fi
check "$disp" '^Environment=PANEL_SECRETS_DIR=/var/lib/harness/panel-state$' "dispatcher: PANEL_SECRETS_DIR в шаблоне"

# ── функции установщика достаются парсингом исходника (пустое извлечение — провал) ──
extract() { awk -v f="$1" 'index($0, f "() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src"; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
fns=""
for f in запуск_от_агента личность_коммитов зафиксировать_пакет журнал_пакета_заготовка первый_коммит_каркаса правила_чтения_sudoers свои_юниты правила_юнитов_sudoers правило_перезагрузки подсети_docker закрыть_ssh_из_docker включить_таймеры все_таймеры_включены копия_прав_для_гейта узнать_chat_id проверить_токен вебхук_бота спросить_токен предупредить_о_вебхуке записать_sudoers дополнить_settings_json ос_поддерживается проверить_ос_установки дождаться_cloud_init; do
  body=$(extract "$f")
  [ -n "$body" ] || { echo "FAIL: функция $f не найдена в установщике"; exit 1; }
  fns+="$body"$'\n'
done
units=$(grep -m1 '^JOURNAL_UNITS=' "$src"); lens=$(grep -m1 '^JOURNAL_LENGTHS=' "$src")
[ -n "$units" ] && [ -n "$lens" ] || { echo "FAIL: списки юнитов журнала не найдены"; exit 1; }
{ echo 'скажи() { printf "%s\n" "$*"; }'; echo "$units"; echo "$lens"; echo "$fns"; } > "$tmp/lib.sh"

# sudoers: стенд команд без root; чтение-only, без звёздочек, проходит visudo -cf
mkdir -p "$tmp/bin"
for c in ufw ss journalctl; do printf '#!/bin/sh\n' > "$tmp/bin/$c"; chmod +x "$tmp/bin/$c"; done
rules=$(PATH="$tmp/bin:/usr/bin:/bin" bash -c ". '$tmp/lib.sh'; правила_чтения_sudoers agent")
check "$rules" "$tmp/bin/ufw status," "sudoers: ufw status точным путём"
check "$rules" "$tmp/bin/ufw status verbose" "sudoers: ufw status verbose"
check "$rules" "$tmp/bin/ss -ltnp" "sudoers: ss -ltnp"
check "$rules" "$tmp/bin/journalctl -u harness-panel --no-pager -n 50" "sudoers: journalctl юнита с фиксированной длиной"
if grep -q '\*' <<<"$rules"; then echo "FAIL: в sudoers есть звёздочка (дверь на запись)"; fail=1; else echo "ok:   sudoers без звёздочек"; fi
printf '%s\n' "$rules" > "$tmp/ro.sudoers"
vs=$(command -v visudo || ls /usr/sbin/visudo 2>/dev/null || true)
if [ -n "$vs" ]; then
  if "$vs" -cf "$tmp/ro.sudoers" >/dev/null 2>&1; then echo "ok:   sudoers проходит visudo -cf"; else echo "FAIL: visudo -cf отверг правила"; fail=1; fi
else echo "SKIP: visudo недоступен — синтаксис не проверен"; fi
rm "$tmp/bin/ufw"
nou=$(PATH="$tmp/bin:/usr/bin:/bin" bash -c ". '$tmp/lib.sh'; правила_чтения_sudoers agent")
if grep -q 'ufw' <<<"$nou"; then echo "FAIL: без ufw правила на него остались"; fail=1; else echo "ok:   без ufw правил на него нет"; fi
rm "$tmp/bin/journalctl"
if PATH="$tmp/bin" /bin/bash -c ". '$tmp/lib.sh'; правила_чтения_sudoers agent" >/dev/null 2>&1; then
  echo "FAIL: без journalctl функция не отказала"; fail=1; else echo "ok:   без journalctl отказ"; fi

# первый коммит: хук пускает ТОЛЬКО через честную дверь пайплайна
mkrepo() { # каталог; хук-проба как pre-commit
  git init -q "$1"; mkdir -p "$1/scripts"; echo "каркас" > "$1/scripts/x.sh"
  printf '#!/bin/sh\n[ "$HARNESS_PIPELINE_FORCE" = 1 ] && [ -n "$HARNESS_PIPELINE_WHY" ] || { echo "красный"; exit 1; }\n' > "$1/scripts/hook.sh"
  chmod +x "$1/scripts/hook.sh"; ln -s ../../scripts/hook.sh "$1/.git/hooks/pre-commit"
}
run() { env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 AS_AGENT= bash -c ". '$tmp/lib.sh'; $1"; }
mkrepo "$tmp/p1"
run "первый_коммит_каркаса '$tmp/p1' me" >/dev/null 2>&1; rc=$?
if [ $rc = 0 ] && git -C "$tmp/p1" rev-parse -q --verify HEAD >/dev/null; then echo "ok:   первый коммит прошёл дверью"; else echo "FAIL: первый коммит не сделан (rc=$rc)"; fail=1; fi
check "$(cat "$tmp/p1/docs/ИЗМЕНЕНИЯ-ПАКЕТА.md" 2>/dev/null)" "^\| $(date +%d.%m) \|" "журнал пакета: заготовка с записью за сегодня"
head1=$(git -C "$tmp/p1" rev-parse HEAD)
echo "правка" >> "$tmp/p1/scripts/x.sh"
run "первый_коммит_каркаса '$tmp/p1' me" >/dev/null 2>&1
[ "$(git -C "$tmp/p1" rev-parse HEAD)" = "$head1" ] && echo "ok:   повтор не делает второго коммита" || { echo "FAIL: повтор сделал коммит"; fail=1; }
mkrepo "$tmp/p2"; printf '#!/bin/sh\nexit 1\n' > "$tmp/p2/scripts/hook.sh"
if run "первый_коммит_каркаса '$tmp/p2' me" >/dev/null 2>&1; then echo "FAIL: красный хук, а функция вернула 0"; fail=1; else echo "ok:   красный хук даёт ненулевой код"; fi
mkrepo "$tmp/p3"; rm "$tmp/p3/.git/hooks/pre-commit"
if run "первый_коммит_каркаса '$tmp/p3' me" >/dev/null 2>&1; then echo "FAIL: без pre-commit коммит сделан"; fail=1; else echo "ok:   без подключённого pre-commit коммит не делается"; fi
mkdir -p "$tmp/pkg/harness/scripts"; echo a > "$tmp/pkg/f"
cp "$here/scripts/check-secrets.sh" "$tmp/pkg/harness/scripts/" 2>/dev/null || cp "$(dirname "$0")/check-secrets.sh" "$tmp/pkg/harness/scripts/"
run "зафиксировать_пакет me '$tmp/pkg'" >/dev/null 2>&1
[ -z "$(git -C "$tmp/pkg" status --porcelain 2>/dev/null)" ] && git -C "$tmp/pkg" rev-parse -q --verify HEAD >/dev/null \
  && echo "ok:   пакет без .git получил историю и чист" || { echo "FAIL: пакет не зафиксирован"; fail=1; }
# секрет в пакете: коммит отказан, индекс пакета возвращён
mkdir -p "$tmp/pkgs/harness/scripts"; cp "$tmp/pkg/harness/scripts/check-secrets.sh" "$tmp/pkgs/harness/scripts/"
printf 'password = supersecret123456\n' > "$tmp/pkgs/cfg.env"  # не-секрет: подложенный секрет стенда, гейт обязан его поймать
if run "зафиксировать_пакет me '$tmp/pkgs'" >/dev/null 2>&1; then echo "FAIL: пакет с секретом зафиксирован"; fail=1
elif git -C "$tmp/pkgs" rev-parse -q --verify HEAD >/dev/null 2>&1; then echo "FAIL: коммит с секретом всё же сделан"; fail=1
else echo "ok:   пакет с секретом не коммитится"; fi

# ── установка без sudo владельца: юниты, reboot, ufw, таймеры, копия прав ──
vcf() { # файл sudoers, описание → visudo -cf (нет visudo — честный пропуск)
  if [ -n "$vs" ]; then "$vs" -cf "$1" >/dev/null 2>&1 && echo "ok:   $2 проходит visudo -cf" || { echo "FAIL: $2 отвергнут visudo"; fail=1; }
  else echo "SKIP: visudo недоступен ($2)"; fi
}
bin="$tmp/bin2"; mkdir -p "$bin"
for c in ufw ss journalctl systemctl reboot; do printf '#!/bin/sh\n' > "$bin/$c"; chmod +x "$bin/$c"; done
runp() { PATH="$bin:/usr/bin:/bin" bash -c ". '$tmp/lib.sh'; $1"; }
check "$(runp 'правила_чтения_sudoers agent')" "$bin/ufw status numbered" "sudoers: ufw status numbered"
# юниты: список собирается парсингом установщика и шаблонов пакета
mkdir -p "$tmp/hp/panel"; : > "$tmp/hp/panel/harness-panel.service.in"; : > "$tmp/hp/panel/nginx-panel.conf.in"
units=$(runp "свои_юниты '$src' '$tmp/hp' acceptance-watch checks-stream")
check "$units" "^harness-dispatcher.service$" "юниты: dispatcher взят из установщика"
check "$units" "^harness-sentinel.timer$" "юниты: sentinel.timer взят из установщика"
check "$units" "^harness-panel.service$" "юниты: панель взята из шаблона пакета"
check "$units" "^harness-acceptance-watch.timer$" "юниты: таймер службы проверок"
if grep -q 'nginx' <<<"$units"; then echo "FAIL: в юниты попал не-юнит шаблона"; fail=1; else echo "ok:   в юнитах нет лишнего"; fi
nu=$(grep -c . <<<"$units"); [ "$nu" -ge 8 ] && echo "ok:   юнитов $nu" || { echo "FAIL: мало юнитов ($nu)"; fail=1; }
runp "свои_юниты /nonexistent /nonexistent" >/dev/null 2>&1 && { echo "FAIL: пустой список юнитов не отказ"; fail=1; } || echo "ok:   пустой список юнитов — отказ"
mapfile -t ul <<<"$units"
ur=$(runp "правила_юнитов_sudoers agent ${ul[*]}")
printf '%s\n' "$ur" > "$tmp/units.sudoers"; vcf "$tmp/units.sudoers" "sudoers юнитов"
check "$ur" "$bin/systemctl restart harness-dispatcher.service" "sudoers юнитов: restart точным именем"
check "$ur" "$bin/systemctl status harness-panel.service" "sudoers юнитов: status"
if grep -q '\*' <<<"$ur"; then echo "FAIL: звёздочка в sudoers юнитов"; fail=1; else echo "ok:   sudoers юнитов без звёздочки"; fi
if grep -qE 'systemctl (enable|disable|daemon-reload|edit|mask)' <<<"$ur"; then echo "FAIL: лишний глагол в sudoers юнитов"; fail=1; else echo "ok:   только restart/start/stop/status"; fi
# рестарт, который делает update.py, обязан совпасть с правилом БУКВА В БУКВУ: sudo сверяет аргументы точно,
# и `restart harness-panel` не равно `restart harness-panel.service`
upd="${UPDATE_UNDER_TEST:-$here/scripts/update.py}"
restarted=$(grep 'for служба in (' "$upd" | grep -o '"harness-[a-z.-]*"' | tr -d '"')
[ -n "$restarted" ] || { echo "FAIL: список рестартуемых служб update.py не найден"; fail=1; }
for u in $restarted; do
  check "$ur" "$bin/systemctl restart $u(,|\$| )" "sudoers юнитов: update.py рестартует $u — право есть"
done
# reboot: только по ключу
printf 'AUTO_REBOOT_IN_ROTATION=1\n' > "$tmp/on.conf"; printf 'AUTO_REBOOT_IN_ROTATION=0\n' > "$tmp/off.conf"; printf 'X=1\n' > "$tmp/none.conf"
rb=$(runp "правило_перезагрузки agent '$tmp/on.conf'")
check "$rb" "NOPASSWD: $bin/reboot \"\"\$" "reboot: ключ 1 — точный путь и только без аргументов"
printf '%s\n' "$rb" > "$tmp/rb.sudoers"; vcf "$tmp/rb.sudoers" "sudoers reboot"
for c in off none; do
  [ -z "$(runp "правило_перезагрузки agent '$tmp/$c.conf'")" ] && echo "ok:   reboot: ключ $c — права нет" || { echo "FAIL: reboot выдан при ключе $c"; fail=1; }
done
[ -z "$(runp "правило_перезагрузки agent '$tmp/net.conf'")" ] && echo "ok:   reboot: нет конфига — права нет" || { echo "FAIL: reboot без конфига"; fail=1; }
# ufw + docker: заглушки; ufw помнит вызовы и отдаёт status из файла, docker отдаёт подсети из файла
cat > "$bin/ufw" <<EOF
#!/bin/sh
echo "\$*" >> "$tmp/ufw.calls"
[ "\$1" = status ] && cat "$tmp/ufw.state"
exit 0
EOF
cat > "$bin/docker" <<EOF
#!/bin/sh
case "\$2" in ls) echo n1; echo n2 ;; inspect) cat "$tmp/docker.subnets" ;; esac
EOF
chmod +x "$bin/ufw" "$bin/docker"
printf '172.17.0.0/16\n192.168.16.0/20\nfd00::/64\n' > "$tmp/docker.subnets"
: > "$tmp/ufw.calls"
printf 'Status: active\n\nTo   Action  From\n22/tcp  ALLOW  Anywhere\n' > "$tmp/ufw.state"
runp закрыть_ssh_из_docker >/dev/null; rc=$?
ins=$(grep -c '^insert 1 deny proto tcp from [0-9./]* to any port 22$' "$tmp/ufw.calls")
[ $rc = 0 ] && [ "$ins" = 2 ] && grep -qx 'insert 1 deny proto tcp from 192.168.16.0/20 to any port 22' "$tmp/ufw.calls" && echo "ok:   ufw: правила по фактическим подсетям (в т.ч. 192.168/16), IPv6 пропущен" || { echo "FAIL: ufw правила по подсетям (rc=$rc, вставок $ins)"; fail=1; }
grep -q '172.16.0.0/12' "$tmp/ufw.calls" && { echo "FAIL: ufw: широкий 172.16/12 остался"; fail=1; } || echo "ok:   ufw: широкого диапазона нет"
[ "$(grep -vc '^status\|^insert' "$tmp/ufw.calls")" = 0 ] && echo "ok:   ufw: ничего кроме status/insert" || { echo "FAIL: ufw: лишние вызовы"; fail=1; }
: > "$tmp/ufw.calls"; printf 'Status: active\n\n22/tcp  DENY  172.17.0.0/16\n22/tcp  DENY  192.168.16.0/20\n' > "$tmp/ufw.state"
runp закрыть_ssh_из_docker >/dev/null
grep -q '^insert' "$tmp/ufw.calls" && { echo "FAIL: правило продублировано"; fail=1; } || echo "ok:   ufw: повтор не дублирует"
: > "$tmp/ufw.calls"; printf 'Status: active\n\n22/tcp  DENY  172.17.0.0/16\n' > "$tmp/ufw.state"
runp закрыть_ssh_из_docker >/dev/null
[ "$(grep -c '^insert' "$tmp/ufw.calls")" = 1 ] && grep -q '192.168.16.0/20' "$tmp/ufw.calls" && echo "ok:   ufw: добавлена только недостающая подсеть" || { echo "FAIL: ufw: частичное состояние"; fail=1; }
: > "$tmp/ufw.calls"; printf 'Status: inactive\n' > "$tmp/ufw.state"
runp закрыть_ssh_из_docker >/dev/null; rc=$?
[ $rc = 2 ] && ! grep -q '^insert' "$tmp/ufw.calls" && echo "ok:   ufw неактивен — rc 2, правил нет" || { echo "FAIL: неактивный ufw (rc=$rc)"; fail=1; }
: > "$tmp/ufw.calls"; printf 'Status: active\n' > "$tmp/ufw.state"; printf '#!/bin/sh\nexit 1\n' > "$bin/docker"
out=$(runp закрыть_ssh_из_docker); rc=$?
[ $rc = 3 ] && ! grep -q '^insert' "$tmp/ufw.calls" && echo "ok:   docker не отвечает — rc 3, правил нет" || { echo "FAIL: нет docker (rc=$rc)"; fail=1; }
rm "$bin/ufw"; out=$(runp закрыть_ssh_из_docker); rc=$?
[ $rc = 2 ] && grep -q 'не установлен' <<<"$out" && echo "ok:   нет ufw — rc 2 и слова для сводки" || { echo "FAIL: нет ufw (rc=$rc)"; fail=1; }
# таймеры: заглушка systemctl, состояние — файлы
cat > "$bin/systemctl" <<EOF
#!/bin/sh
case "\$1" in
  enable) [ "\$2" = --now ] && shift; touch "$tmp/en.\$2" ;;
  is-enabled) [ "\$2" = --quiet ] && shift; [ -f "$tmp/en.\$2" ] ;;
esac
EOF
chmod +x "$bin/systemctl"
tl="harness-a.timer harness-b.timer harness-a.service"
runp "все_таймеры_включены $tl" >/dev/null 2>&1 && { echo "FAIL: проба зелёная при выключенных таймерах"; fail=1; } || echo "ok:   проба таймеров красная, пока не включены"
runp "включить_таймеры $tl" >/dev/null
runp "все_таймеры_включены $tl" >/dev/null 2>&1 && echo "ok:   после enable --now проба зелёная" || { echo "FAIL: проба красная после включения"; fail=1; }
[ ! -f "$tmp/en.harness-a.service" ] && echo "ok:   сервисы не включались" || { echo "FAIL: включён service"; fail=1; }
rm -f "$tmp/en.harness-b.timer"
out=$(runp "все_таймеры_включены $tl" 2>&1); grep -q 'harness-b.timer' <<<"$out" && echo "ok:   проба называет невключённый таймер" || { echo "FAIL: проба не назвала таймер"; fail=1; }
# копия прав: все файлы каталога, с заголовками
mkdir -p "$tmp/sd"; echo 'agent ALL=(root) NOPASSWD: /bin/true' > "$tmp/sd/50-agent"; echo 'panel ALL=(root) NOPASSWD: /bin/false' > "$tmp/sd/harness-panel"
cp_=$(runp "копия_прав_для_гейта '$tmp/sd'")
check "$cp_" "NOPASSWD: /bin/true" "копия прав: файл агента"
check "$cp_" "NOPASSWD: /bin/false" "копия прав: файл панели"
[ "$(grep -c '^# файл:' <<<"$cp_")" = 2 ] && echo "ok:   копия прав: по заголовку на файл" || { echo "FAIL: заголовки копии"; fail=1; }
# шаг 12 выдаёт права и идёт при --resume; шаг 1 их больше не выдаёт; группа adm не выдаётся
body12=$(awk 'index($0,"шаг_12() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
body1=$(awk 'index($0,"шаг_1() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
[ -n "$body12" ] && [ -n "$body1" ] || { echo "FAIL: тела шагов 1/12 не извлечены"; fail=1; }
for w in harness-agent-readonly harness-agent-units закрыть_ssh_из_docker правило_перезагрузки "usermod -aG systemd-journal" /etc/harness/sudo-rights.txt; do
  check "$body12" "$w" "шаг 12 выдаёт: $w"
done
if grep -qE 'harness-agent-(readonly|units)|usermod -aG (docker|harness)|закрыть_ssh' <<<"$body1"; then echo "FAIL: шаг 1 всё ещё выдаёт права (--resume их не довыдаст)"; fail=1; else echo "ok:   шаг 1 права не выдаёт"; fi
if grep -qE '(^|[[:space:]])(отметить|уже_сделан) ' <<<"$body12"; then echo "FAIL: у шага 12 есть отметка — --resume его пропустит"; fail=1; else echo "ok:   шаг 12 без отметки (идёт при --resume)"; fi
if grep -qE 'usermod[^"]*adm( |$)|journal adm|,adm' <<<"$body12"; then echo "FAIL: группа adm выдаётся"; fail=1; else echo "ok:   группа adm не выдаётся"; fi
if grep -q 'agent/sudo-rights' <<<"$body12"; then echo "FAIL: копия в каталоге агента"; fail=1; else echo "ok:   копия прав вне каталога агента"; fi
# установщик: av<15 при установке faster-whisper, шаг 12 в прогоне
grep -q "install --only-binary=av faster-whisper 'av<15'" "$src" && echo "ok:   faster-whisper ставится с av<15" || { echo "FAIL: нет av<15"; fail=1; }
[ "$(grep -cx 'шаг_12' "$src")" = 1 ] && echo "ok:   шаг_12 в прогоне" || { echo "FAIL: шаг_12 не в прогоне"; fail=1; }

# ── правки по разбору сценариев установки ──────────────────────────────────
b3="$tmp/bin3"; mkdir -p "$b3"
runq() { PATH="$b3:/usr/bin:/bin" bash -c ". '$tmp/lib.sh'; $1"; }
dock="$here/scripts/install-docker.sh"
# 1. apt: функция-обёртка с ожиданием замка, без диалогов, cloud-init
grep -q '^export DEBIAN_FRONTEND=noninteractive' "$src" && echo "ok:   DEBIAN_FRONTEND экспортирован" || { echo "FAIL: нет export DEBIAN_FRONTEND"; fail=1; }
aptline=$(grep -m1 '^apt-get() {' "$src")
[ -n "$aptline" ] || { echo "FAIL: обёртка apt-get не найдена"; fail=1; }
printf '#!/bin/sh\necho "$*" >> "%s/apt.calls"\n' "$tmp" > "$b3/apt-get"; chmod +x "$b3/apt-get"; : > "$tmp/apt.calls"
PATH="$b3:/usr/bin:/bin" bash -c "APT_LOCK_WAIT=600; $aptline; apt-get install -y x; apt-get update"
[ "$(grep -c 'DPkg::Lock::Timeout=600' "$tmp/apt.calls")" = 2 ] && echo "ok:   каждый вызов apt-get идёт с DPkg::Lock::Timeout" || { echo "FAIL: apt-get без ожидания замка"; fail=1; }
dockline=$(grep -m1 '^apt-get() {' "$dock"); : > "$tmp/apt.calls"
PATH="$b3:/usr/bin:/bin" bash -c "$dockline; apt-get install -y x"
grep -q 'DPkg::Lock::Timeout=600' "$tmp/apt.calls" && grep -q '^    if ! apt-get install' "$dock" && echo "ok:   install-docker.sh: apt-get только с ожиданием замка" || { echo "FAIL: install-docker.sh зовёт голый apt-get"; fail=1; }
printf '#!/bin/sh\necho "$*" >> "%s/ci.calls"\n' "$tmp" > "$b3/cloud-init"; chmod +x "$b3/cloud-init"; : > "$tmp/ci.calls"
runq "дождаться_cloud_init"; grep -qx 'status --wait' "$tmp/ci.calls" && echo "ok:   cloud-init status --wait зван" || { echo "FAIL: cloud-init не ждали"; fail=1; }
rm "$b3/cloud-init"; mkdir -p "$tmp/b4"; ln -sf "$(command -v timeout)" "$tmp/b4/timeout"
PATH="$tmp/b4" /bin/bash -c ". '$tmp/lib.sh'; дождаться_cloud_init" && echo "ok:   нет cloud-init — без ожидания, rc 0" || { echo "FAIL: без cloud-init функция упала"; fail=1; }
# 2. chat_id: функция разбирается bash'ем, на stdout ТОЛЬКО число
printf '#!/bin/sh\ncat "%s/tg.reply"\n' "$tmp" > "$b3/curl"; printf '#!/bin/sh\nexit 0\n' > "$b3/sleep"; chmod +x "$b3/curl" "$b3/sleep"
printf '{"ok":true,"result":[{"message":{"chat":{"id":424242}}}]}' > "$tmp/tg.reply"
got=$(runq "узнать_chat_id tok 2>/dev/null"); [ "$got" = 424242 ] && echo "ok:   chat_id: на stdout только число" || { echo "FAIL: chat_id на stdout: «$got»"; fail=1; }
err=$(runq "узнать_chat_id tok 2>&1 >/dev/null"); grep -q 'chat_id получен' <<<"$err" && echo "ok:   chat_id: подсказки ушли в stderr" || { echo "FAIL: подсказки не в stderr"; fail=1; }
printf '{"ok":true,"result":[]}' > "$tmp/tg.reply"
got=$(runq "узнать_chat_id tok 2>/dev/null"); rc=$?
[ -z "$got" ] && echo "ok:   chat_id: нет сообщения — пусто на stdout" || { echo "FAIL: chat_id без сообщения дал «$got»"; fail=1; }
# 3. токен: getMe, getWebhookInfo, переспрашивание
printf '#!/bin/sh\nread -r in; case "$in" in *good*) cat "%s/me.good" ;; *) cat "%s/me.bad" ;; esac\n' "$tmp" "$tmp" > "$b3/curl"
printf '{"ok":true,"result":{"id":1}}' > "$tmp/me.good"; printf '{"ok":false,"error_code":401,"description":"Unauthorized"}' > "$tmp/me.bad"
runq "проверить_токен good" && echo "ok:   getMe: верный токен принят" || { echo "FAIL: верный токен отвергнут"; fail=1; }
runq "проверить_токен bad"; [ $? = 1 ] && echo "ok:   getMe: неверный токен — rc 1" || { echo "FAIL: неверный токен не rc 1"; fail=1; }
printf 'не json' > "$tmp/me.bad"; runq "проверить_токен bad"; [ $? = 2 ] && echo "ok:   getMe: не-JSON — rc 2 (связи нет, не отказ токена)" || { echo "FAIL: не-JSON дал не rc 2"; fail=1; }
printf '{"ok":false,"error_code":401}' > "$tmp/me.bad"
out=$(printf 'bad\n\ngood\n' | runq "спросить_токен >/dev/null 2>&1; echo \$BOT_TOKEN")
[ "$out" = good ] && echo "ok:   неверный токен переспрашивается, пустой пропускается" || { echo "FAIL: спросить_токен вернул «$out»"; fail=1; }
printf '#!/bin/sh\ncat "%s/wh.reply"\n' "$tmp" > "$b3/curl"
printf '{"ok":true,"result":{"url":"https://old.example/hook"}}' > "$tmp/wh.reply"
check "$(runq 'вебхук_бота tok')" "old.example" "getWebhookInfo: вебхук назван"
printf '{"ok":true,"result":{"url":""}}' > "$tmp/wh.reply"
[ -z "$(runq 'вебхук_бота tok')" ] && echo "ok:   getWebhookInfo: вебхука нет — пусто" || { echo "FAIL: пустой вебхук дал вывод"; fail=1; }
printf '{"ok":true,"result":{"url":"https://old.example/hook"}}' > "$tmp/wh.reply"
out=$(runq 'MANUAL_LIST=(); TG_API=x; предупредить_о_вебхуке tok; echo "${#MANUAL_LIST[@]}"' | tail -1); [ "$out" = 1 ] && echo "ok:   вебхук: предупреждение и строка в «осталось руками»" || { echo "FAIL: вебхук без предупреждения"; fail=1; }
rm "$b3/curl" "$b3/sleep"
# 4. окружение не обнуляется (прогон --dry-run установщика), PRODUCT_PATH доходит до install.conf
out=$(PROJECT_DIR=/srv/custom SECRETS_DIR=/srv/sec AGENT_USER=bob PRODUCT_PATH=/srv/prod HARNESS_INSTALL_CONF="$tmp/none/install.conf" bash "$src" --dry-run 2>&1); rc=$?
[ $rc = 0 ] && echo "ok:   --dry-run с окружением: rc 0" || { echo "FAIL: --dry-run rc=$rc"; fail=1; }
check "$out" "PROJECT_DIR=/srv/custom SECRETS_DIR=/srv/sec" "окружение: PROJECT_DIR и SECRETS_DIR не обнулены"
check "$out" 'AGENT_USER="bob"' "окружение: AGENT_USER не обнулён"
check "$out" 'PRODUCT_PATH="/srv/prod"' "install.conf получает PRODUCT_PATH"
check "$out" "\[ -d '/srv/custom' \] \|\| \{ mkdir -p '/srv/custom' && chown bob:bob '/srv/custom'; \}" "chown только самого каталога и только при создании"
if grep -qE "chown -R [^']*'/srv/custom'" <<<"$out"; then echo "FAIL: рекурсивный chown PROJECT_DIR остался"; fail=1; else echo "ok:   рекурсивного chown PROJECT_DIR нет"; fi
out=$(HARNESS_INSTALL_CONF="$tmp/none2/install.conf" bash "$src" --dry-run 2>&1); [ $? = 0 ] && check "$out" "PROJECT_DIR=/opt/myproject" "без окружения — прежние умолчания" || { echo "FAIL: --dry-run без окружения"; fail=1; }
# 5. шаги: перезапуск по смене группы, раскладка без пропуска, шаг 8 мягкий
body10=$(awk 'index($0,"шаг_10() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
body7a=$(awk 'index($0,"шаг_7а() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
body8=$(awk 'index($0,"шаг_8() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
check "$(grep -B1 'try-restart' <<<"$body10")" "panel_group_new" "шаг 10: перезапуск только если группа выдана сейчас"
if grep -q 'уже_сделан шаг_7а' <<<"$body7a"; then echo "FAIL: раскладка пропускается по отметке"; fail=1; else echo "ok:   раскладка не пропускается по отметке"; fi
check "$body7a" "положить_пакет '" "шаг 7-а: копия пакета рядом перед затиранием (в положить_пакет; пробы — test_installer_paths.sh)"
check "$body8" "FAILED_LIST" "шаг 8: сбой плагина идёт в сводку"
if grep -q 'проверка "\$step" "базовый набор' <<<"$body8"; then echo "FAIL: сбой плагина всё ещё стоп"; fail=1; else echo "ok:   сбой плагина не стоп"; fi
check "$(awk 'index($0,"шаг_1() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")" "usermod -aG systemd-journal" "группа журнала выдаётся в шаге 1, до перезапусков"
# 6. settings.json: дополняется, не переписывается
sj="$tmp/settings.json"
printf '{"language":"english","enabledPlugins":{"a@b":true},"permissions":{"allow":["Bash(ls)"],"defaultMode":"ask"}}' > "$sj"
runp "дополнить_settings_json '$sj'" >/dev/null
python3 -I - "$sj" <<'PY' && echo "ok:   settings.json: ключи дописаны, enabledPlugins и allow целы" || { echo "FAIL: settings.json стёрт или не дописан"; fail=1; }
import json, sys
д = json.load(open(sys.argv[1]))
assert д["language"] == "russian" and д["permissions"]["defaultMode"] == "bypassPermissions"
assert д["enabledPlugins"] == {"a@b": True} and д["permissions"]["allow"] == ["Bash(ls)"]
PY
printf '{сломано' > "$tmp/bad.json"; runp "дополнить_settings_json '$tmp/bad.json'" >/dev/null
[ -f "$tmp/bad.json.bak-broken" ] && python3 -I -c "import json,sys;json.load(open(sys.argv[1]))" "$tmp/bad.json" && echo "ok:   битый settings.json: копия рядом, результат валиден" || { echo "FAIL: битый settings.json"; fail=1; }
runp "дополнить_settings_json '$tmp/new.json'" >/dev/null; [ -s "$tmp/new.json" ] && echo "ok:   нет settings.json — создан" || { echo "FAIL: settings.json не создан"; fail=1; }
# 7. ОС
printf 'ID=ubuntu\nVERSION_ID="24.04"\n' > "$tmp/os24"; printf 'ID=ubuntu\nVERSION_ID="22.04"\n' > "$tmp/os22"; printf 'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n' > "$tmp/osdeb"; printf 'ID=ubuntu\nVERSION_ID="20.04"\n' > "$tmp/os20"
for o in os24 os22; do runp "ос_поддерживается '$tmp/$o'" && echo "ok:   ОС $o принята" || { echo "FAIL: ОС $o отвергнута"; fail=1; }; done
for o in osdeb os20 нет; do runp "ос_поддерживается '$tmp/$o'" && { echo "FAIL: ОС $o принята"; fail=1; } || echo "ok:   ОС $o не принята"; done
out=$(runp "DRY=0; проверить_ос_установки '$tmp/osdeb'" </dev/null 2>&1); rc=$?
[ $rc = 1 ] && grep -q 'Debian 12' <<<"$out" && grep -q 'спросить некого' <<<"$out" && echo "ok:   чужая ОС без терминала — выход с причиной" || { echo "FAIL: чужая ОС без терминала (rc=$rc)"; fail=1; }
runp "DRY=1; проверить_ос_установки '$tmp/osdeb'" </dev/null >/dev/null 2>&1 && echo "ok:   чужая ОС в --dry-run — только предупреждение" || { echo "FAIL: dry-run остановлен чужой ОС"; fail=1; }
# 8. sudoers 50-agent: временный файл, visudo, install; обрыв SSH и лог
printf '#!/bin/sh\nfor a; do last="$a"; prev2="${prev:-}"; prev="$a"; done\ncp "$prev2" "$last"\n' > "$b3/install"
printf '#!/bin/sh\nexit 0\n' > "$b3/visudo"; chmod +x "$b3/install" "$b3/visudo"
if [ -n "$vs" ]; then ln -sf "$vs" "$b3/visudo"; fi
runv() { PATH="$b3:/usr/bin:/bin" bash -c ". '$tmp/lib.sh'; стоп() { echo \"СТОП: \$2\"; exit 1; }; CURRENT_STEP=x; DRY=\${DRY:-0}; $1"; }
printf 'agent ALL=(root) NOPASSWD: /bin/true\n' | runv "записать_sudoers '$tmp/ok.sudoers'" >/dev/null 2>&1
[ -s "$tmp/ok.sudoers" ] && echo "ok:   sudoers: проверенный файл установлен" || { echo "FAIL: sudoers не установлен"; fail=1; }
if [ -n "$vs" ]; then
  printf 'agent ALL=(root NOPASSWD: ((\n' | runv "записать_sudoers '$tmp/bad.sudoers'" >/dev/null 2>&1
  [ ! -e "$tmp/bad.sudoers" ] && echo "ok:   битый sudoers до sudoers.d не доходит" || { echo "FAIL: битый sudoers установлен"; fail=1; }
fi
printf 'agent ALL=(root) NOPASSWD: /bin/true\n' | DRY=1 runv "записать_sudoers '$tmp/dry.sudoers'" >/dev/null 2>&1
[ ! -e "$tmp/dry.sudoers" ] && echo "ok:   sudoers в --dry-run не пишется" || { echo "FAIL: dry-run записал sudoers"; fail=1; }
check "$(grep -n "записать_sudoers /etc/sudoers.d/50-agent" "$src")" "50-agent" "50-agent пишется через записать_sudoers"
first_step=$(grep -n '^шаг_0$' "$src" | cut -d: -f1); hup=$(grep -n "^  trap '' HUP" "$src" | cut -d: -f1); teel=$(grep -n 'tee -a "\$LOG_DIR/install.log"' "$src" | cut -d: -f1)
[ -n "$hup" ] && [ -n "$teel" ] && [ "$hup" -lt "$first_step" ] && [ "$teel" -lt "$first_step" ] && echo "ok:   trap '' HUP и лог установки — до первого шага" || { echo "FAIL: нет HUP/лога до шагов"; fail=1; }
check "$(grep -n 'tmux или screen' "$src")" "tmux" "подсказка про tmux/screen в начале"
check "$(grep -n -- '--max-time 20' "$src")" "max-time 20" "sendMessage с --max-time 20"
# 9. python 3.10: datetime.UTC нет ни в двух правленых файлах, ни где-либо ещё в коде
if grep -rnE 'datetime\.UTC|from datetime import .*\bUTC\b' "$here" --include=*.py | grep -v '/test_' | grep -q .; then echo "FAIL: datetime.UTC (python 3.11+) остался в коде"; fail=1; else echo "ok:   datetime.UTC в коде нет"; fi

# ── по живой установке в контейнере ────────────────────────────────────────
# Д-1а: каталог установщика (/root/...) агенту не виден — после пролога cwd это «/», SELF абсолютен
awk '/^SCRIPT_DIR=/ {on=1} on {print} on && /^cd \/$/ {exit}' "$src" > "$tmp/prolog.sh"
[ "$(wc -l < "$tmp/prolog.sh")" -ge 2 ] || { echo "FAIL: пролог с cd / не найден"; fail=1; }
mkdir -p "$tmp/rel"; cp "$tmp/prolog.sh" "$tmp/rel/p.sh"; echo 'echo "cwd=$(pwd) self=$SELF"' >> "$tmp/rel/p.sh"
out=$(cd "$tmp/rel" && bash ./p.sh)
[ "$out" = "cwd=/ self=$tmp/rel/p.sh" ] && echo "ok:   после пролога cwd=/ и SELF абсолютен (relative-запуск)" || { echo "FAIL: пролог: «$out»"; fail=1; }
if grep -nE '[$]0|BASH_SOURCE' "$src" | grep -vE 'SCRIPT_DIR=|SELF=|^[0-9]+:[[:space:]]*#' | grep -q .; then echo "FAIL: после cd / остались \$0/BASH_SOURCE (относительный путь в подсказках)"; fail=1; else echo "ok:   подсказки идут через SELF, не через \$0"; fi
# Д-2: готовое колесо av
check "$(grep -n "install --only-binary=av faster-whisper 'av<15'" "$src")" "only-binary=av" "голосовые: av берётся только колесом"
check "$(awk 'index($0,"шаг_2в() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")" "FAILED_LIST" "голосовые: отказ pip идёт в сводку, не стоп"
# Д-4: iproute2 ставится, ловушка ERR кладёт ошибку в FAILED_LIST
check "$(grep -n 'apt_поставить git tmux' "$src")" "iproute2" "шаг 2 ставит iproute2 (нужен ss)"
awk '/^trap .rc=\$\?;/ {on=1} on {print} on && /ERR$/ {exit}' "$src" > "$tmp/errtrap.sh"
[ -s "$tmp/errtrap.sh" ] || { echo "FAIL: ловушка ERR не найдена"; fail=1; }
out=$(bash -c "set -Eeuo pipefail; SELF=x; FAILED_LIST=(); CURRENT_STEP='шаг 12'; CURRENT_CMD=''; . '$tmp/errtrap.sh'; trap 'printf \"FL: %s\n\" \"\${FAILED_LIST[@]}\"' EXIT; false" 2>/dev/null)
check "$out" "FL: ОШИБКА на шаге «шаг 12» \(код 1\)" "ошибка ловушки ERR попадает в FAILED_LIST (блок «УПАЛО»)"
# Д-6: демоны прогоняются ПОСЛЕ первого коммита
body9=$(awk 'index($0,"шаг_9() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
body13=$(awk 'index($0,"шаг_13() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
if grep -q 'env -i' <<<"$body9"; then echo "FAIL: шаг 9 всё ещё прогоняет демонов"; fail=1; else echo "ok:   шаг 9 демонов не прогоняет (только crontab)"; fi
check "$body13" "env -i" "шаг 13 прогоняет демонов в env -i"
n11=$(grep -nx 'шаг_11' "$src" | cut -d: -f1); n13=$(grep -nx 'шаг_13' "$src" | cut -d: -f1)
[ -n "$n11" ] && [ -n "$n13" ] && [ "$n13" -gt "$n11" ] && echo "ok:   шаг_13 стоит в прогоне после шаг_11" || { echo "FAIL: порядок шагов 11/13"; fail=1; }
# Д-7: строка про 22/tcp называет, чего ждём; docker без сетей — отдельный код
body12=$(awk 'index($0,"шаг_12() {")==1 {on=1} on {print} on && /^}$/ {exit}' "$src")
check "$(grep -n '^      2) MANUAL_LIST' <<<"$body12")" "ufw не установлен или не включён" "22/tcp: код 2 называет ufw"
check "$(grep -n '^      3) MANUAL_LIST' <<<"$body12")" "docker не установлен" "22/tcp: код 3 называет docker"
printf '#!/bin/sh\necho "$*" >> "%s/ufw.calls"\n[ "$1" = status ] && printf "Status: active\\n"\nexit 0\n' "$tmp" > "$bin/ufw"; chmod +x "$bin/ufw"
printf '#!/bin/sh\ncase "$2" in ls) echo n1 ;; inspect) : ;; esac\n' > "$bin/docker"; chmod +x "$bin/docker"
runp закрыть_ssh_из_docker >/dev/null; rc=$?
[ $rc = 3 ] && echo "ok:   docker без подсетей — rc 3" || { echo "FAIL: docker без подсетей rc=$rc"; fail=1; }

[ $fail = 0 ] && echo "ВСЁ ЗЕЛЁНОЕ" || echo "КРАСНОЕ"
exit $fail
