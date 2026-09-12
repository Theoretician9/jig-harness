#!/usr/bin/env bash
#
# ustanovit.sh — установка возможности «мобильная-разработка» КОДОМ.
#
# Откуда взято: «Промты для Claude Code: автономная работа с мобильным
# приложением» (части 1–3 и 6) — шесть частей, которые подавались модели по
# одной и выполнялись её вниманием. Здесь они переписаны шагами скрипта.
# Причина ровно та же, что во всём пакете: инструкция из шести частей — правило
# на голом внимании, а значит носителя у неё нет. Из промтов кодом стало всё,
# кроме того, что физически требует человека (браузер, оплата, устройство
# Apple) — это выписано в ЧЕЛОВЕКУ.md и уходит владельцу вложением.
#
# Запускается НЕ напрямую, а через менеджер возможностей (он проверяет
# требования и регистрирует установку):
#   bash harness/scripts/vozmozhnost.sh поставить мобильная-разработка
#
# Флаги: --dry-run печатает шаги, ничего не меняя; --selftest проверяет сам
# скрипт во временном HOME без сети, sudo и Android.
#
# Идемпотентен: повторный прогон обновляет код и настройки и НЕ затирает живые
# значения (APP_DIR/BUILD_CMD/APP_PACKAGE, заполненные первой задачей).
set -Eeuo pipefail

PACK_DIR="${PACK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
DRY=0; SELFTEST="${HARNESS_PACK_SELFTEST:-0}"
case "${1:-}" in
  --dry-run) DRY=1 ;;
  --selftest) : ;;  # обрабатывается ниже, до всего остального
  "") : ;;
  *) echo "неизвестный флаг «$1»"; exit 1 ;;
esac

# ── селфтест: временный HOME, ни сети, ни sudo, ни Android ───────────────────
if [[ "${1:-}" == "--selftest" ]]; then
  T=$(mktemp -d); ok_flag=1
  mkdir -p "$T/home" "$T/log"
  : > "$T/home/.bashrc"
  cat > "$T/install.conf" <<EOF
PROJECT_DIR="$T/proj"
LOG_DIR="$T/log"
SECRETS_DIR="$T/secrets"
AGENT_USER="$(id -un)"
EOF
  run_st() { HOME="$T/home" HARNESS_PACK_SELFTEST=1 HARNESS_INSTALL_CONF="$T/install.conf" \
             PACK_DIR="$PACK_DIR" SUDO_FORBIDDEN=1 bash "$0"; }
  out=$(run_st 2>&1) || { echo "SELFTEST: прогон упал:"; printf '%s\n' "$out" | tail -n 20; rm -rf "$T"; exit 1; }
  # 1. системный шаг выписан файлом, а не выполнен молча
  [[ -f "$T/log/mobile/системный-шаг.sh" ]] || { echo "SELFTEST: системный шаг не выписан файлом"; ok_flag=0; }
  grep -q 'openjdk-17-jdk' "$T/log/mobile/системный-шаг.sh" 2>/dev/null || { echo "SELFTEST: в системном шаге нет пакетов из паспорта"; ok_flag=0; }
  grep -q 'ОСТАЛОСЬ РУКАМИ' <<< "$out" || { echo "SELFTEST: вывод не назвал остаток руками"; ok_flag=0; }
  # 2. конфиг петли собран из паспорта, значения оттуда
  C="$T/home/.config/harness-mobile.conf"
  [[ -f "$C" ]] || { echo "SELFTEST: конфиг петли не создан"; ok_flag=0; }
  grep -q 'EMULATOR_MEM_MB=2048' "$C" 2>/dev/null || { echo "SELFTEST: потолок памяти не взят из паспорта"; ok_flag=0; }
  grep -q 'FTL_TIMEOUT="15m"' "$C" 2>/dev/null || { echo "SELFTEST: таймаут Test Lab не взят из паспорта"; ok_flag=0; }
  grep -q 'APP_DIR=""' "$C" 2>/dev/null || { echo "SELFTEST: ключи приложения должны быть пустыми до первой задачи"; ok_flag=0; }
  # 3. скрипты петли на месте и исполняемы
  for b in emu-start emu-stop emu-status emu-shot emu-logs mobile-loop ftl-run; do
    [[ -x "$T/home/bin/$b" ]] || { echo "SELFTEST: нет исполняемого ~/bin/$b"; ok_flag=0; }
  done
  # 4. блок в .bashrc один и после повтора остаётся одним (идемпотентность)
  printf 'ЖИВАЯ СТРОКА ВЛАДЕЛЬЦА\n' >> "$T/home/.bashrc"
  run_st >/dev/null 2>&1 || true
  cnt=$(grep -c 'harness-mobile: начало' "$T/home/.bashrc" || true)
  [[ "$cnt" == "1" ]] || { echo "SELFTEST: блок .bashrc размножился ($cnt)"; ok_flag=0; }
  grep -q 'ЖИВАЯ СТРОКА ВЛАДЕЛЬЦА' "$T/home/.bashrc" || { echo "SELFTEST: затёрта живая строка .bashrc"; ok_flag=0; }
  # 5. живые значения приложения не затираются повторной установкой
  python3 - "$C" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text(encoding="utf-8")
p.write_text(t.replace('APP_DIR=""', 'APP_DIR="/opt/app"'), encoding="utf-8")
PY
  run_st >/dev/null 2>&1 || true
  grep -q 'APP_DIR="/opt/app"' "$C" || { echo "SELFTEST: повторная установка затёрла живой APP_DIR"; ok_flag=0; }
  # 6. больной случай: паспорта нет — установка обязана отказать
  mkdir -p "$T/пустой"
  cp "$0" "$T/пустой/ustanovit.sh"
  rc=0
  HOME="$T/home" HARNESS_PACK_SELFTEST=1 HARNESS_INSTALL_CONF="$T/install.conf" \
    PACK_DIR="$T/пустой" bash "$T/пустой/ustanovit.sh" >/dev/null 2>&1 || rc=$?
  (( rc != 0 )) || { echo "SELFTEST: установка без паспорта не отказала"; ok_flag=0; }
  rm -rf "$T"
  (( ok_flag )) && { echo "SELFTEST: зелёный (6 проверок, включая больной случай без паспорта)"; exit 0; }
  exit 1
fi

# ── паспорт и окружение ─────────────────────────────────────────────────────
MANIFEST="$PACK_DIR/МАНИФЕСТ.conf"
[[ -r "$MANIFEST" ]] || { echo "нет паспорта $MANIFEST — ставить нечего"; exit 1; }
# shellcheck disable=SC1090
source "$MANIFEST"

INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
if [[ -r "$INSTALL_CONF" ]]; then
  # Окружение старше конфига: общий загрузчик вместо голого source (улика
  # 12.09.2026 — проба с TMUX_SESSION в окружении сменила модель в РАБОЧЕЙ панели).
  # shellcheck disable=SC1091
  source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../../scripts/lib/konf.sh"
  konf_zagruzit
fi
LOG_DIR="${LOG_DIR:-/var/log/harness}"
SECRETS_DIR="${SECRETS_DIR:-/etc/harness/secrets}"
ANDROID_HOME="$HOME/${ANDROID_HOME_REL:-android}"
BIN_DIR="$HOME/bin"
MOBILE_CONF="$HOME/.config/harness-mobile.conf"
STEP=""

trap 'echo "УСТАНОВКА ВСТАЛА на шаге «$STEP» (строка $LINENO). Повтор безопасен: шаги идемпотентны." >&2' ERR

step() { STEP="$1"; printf '\n=== %s\n' "$1"; }
run() { # печатает и выполняет; в --dry-run только печатает
  printf '  + %s\n' "$*"
  (( DRY )) && return 0
  "$@"
}
shell_run() { # то же для строки, которую надо отдать оболочке целиком
  printf '  + %s\n' "$1"
  (( DRY )) && return 0
  bash -c "$1"
}
skip() { printf '  ПРОПУСК: %s\n     адрес: %s\n' "$1" "$2"; }

mkdir -p "$LOG_DIR/mobile" 2>/dev/null || true
HAVE_KVM=0
[[ -r /dev/kvm && -w /dev/kvm ]] && HAVE_KVM=1

# ── 1. системный шаг: единственное окно sudo ─────────────────────────────────
step "1. Системные пакеты (единственное окно sudo)"
SYS_SCRIPT="$LOG_DIR/mobile/системный-шаг.sh"
APT_LIST="${APT_PACKAGES:-}"
[[ -e /dev/kvm ]] && APT_LIST="$APT_LIST ${APT_PACKAGES_KVM:-}"
{
  echo "#!/usr/bin/env bash"
  echo "# Системный шаг возможности «${ID}». Выполняется ОДИН раз, под root."
  echo "# Всё остальное живёт в домашнем каталоге и sudo не требует."
  echo "set -euo pipefail"
  echo "apt-get update"
  echo "DEBIAN_FRONTEND=noninteractive apt-get install -y $APT_LIST"
  echo "# gcloud CLI — репозиторий Google (Firebase Test Lab):"
  echo "install -m 0755 -d /etc/apt/keyrings"
  echo "curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /etc/apt/keyrings/cloud.google.gpg"
  echo "echo 'deb [signed-by=/etc/apt/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main' > /etc/apt/sources.list.d/google-cloud-sdk.list"
  echo "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y google-cloud-cli"
  [[ -e /dev/kvm ]] && echo "usermod -aG kvm ${AGENT_USER:-$(id -un)}"
  echo "echo 'системный шаг выполнен'"
} > "$SYS_SCRIPT"
chmod +x "$SYS_SCRIPT" 2>/dev/null || true

SYS_DONE=0
if [[ "${SUDO_FORBIDDEN:-0}" == "1" ]]; then
  skip "sudo запрещён в этом прогоне (селфтест)" "$SYS_SCRIPT"
elif (( DRY )); then
  printf '  + sudo bash %s\n' "$SYS_SCRIPT"
elif sudo -n true 2>/dev/null; then
  shell_run "sudo bash '$SYS_SCRIPT'" && SYS_DONE=1
else
  skip "sudo без пароля недоступен — системный шаг ВЫПИСАН файлом" \
       "владелец выполняет одной командой: sudo bash $SYS_SCRIPT"
fi

# ── 2. Android SDK: командные инструменты ────────────────────────────────────
step "2. Android SDK (командные инструменты) → $ANDROID_HOME"
SDK_ROOT="$ANDROID_HOME/cmdline-tools/latest"
SDKMANAGER="$SDK_ROOT/bin/sdkmanager"
if [[ "$SELFTEST" == "1" ]]; then
  skip "селфтест: скачивание и sdkmanager не выполняются" "живой прогон на сервере"
elif [[ -x "$SDKMANAGER" ]]; then
  printf '  уже есть: %s\n' "$SDKMANAGER"
else
  ZIP="$HOME/.cache/cmdline-tools.zip"
  run mkdir -p "$HOME/.cache" "$ANDROID_HOME/cmdline-tools"
  run curl -fsSL -o "$ZIP" "${CMDLINE_TOOLS_URL:?пуст CMDLINE_TOOLS_URL в паспорте}"
  if (( ! DRY )); then
    SUM=$(sha256sum "$ZIP" | cut -d' ' -f1)
    if [[ -n "${CMDLINE_TOOLS_SHA256:-}" ]]; then
      [[ "$SUM" == "$CMDLINE_TOOLS_SHA256" ]] || {
        echo "  сумма архива не совпала с паспортом: получено $SUM, ждали $CMDLINE_TOOLS_SHA256"
        echo "  архив НЕ распакован (И-1: неизвестный код не разворачиваем)"; exit 1; }
      printf '  сумма сверена с паспортом: %s\n' "$SUM"
    else
      # Пустая сумма — названная цена, а не незамеченная дыра: печатаем её и
      # требуем вписать в паспорт (пункт ЧЕК-ЛИСТ.md).
      printf '  ВНИМАНИЕ: CMDLINE_TOOLS_SHA256 в паспорте пуст — архив взят без сверки.\n'
      printf '  ВПИСАТЬ В ПАСПОРТ: CMDLINE_TOOLS_SHA256="%s"\n' "$SUM"
    fi
    rm -rf "$ANDROID_HOME/cmdline-tools/tmp"
    unzip -q "$ZIP" -d "$ANDROID_HOME/cmdline-tools/tmp"
    rm -rf "$SDK_ROOT"
    mv "$ANDROID_HOME/cmdline-tools/tmp/cmdline-tools" "$SDK_ROOT"
    rmdir "$ANDROID_HOME/cmdline-tools/tmp" 2>/dev/null || true
  fi
fi

# ── 3. Лицензии и составные части SDK ────────────────────────────────────────
step "3. Лицензии и составные части SDK"
if [[ "$SELFTEST" == "1" ]]; then
  skip "селфтест: sdkmanager не запускается" "живой прогон на сервере"
elif ! (( DRY )) && [[ ! -x "$SDKMANAGER" ]]; then
  skip "sdkmanager не установлен (шаг 2 не прошёл)" "повторить установку после системного шага"
else
  # yes | — иначе sdkmanager ждёт ввода вечно, а сессия агента не интерактивна:
  # висящий вопрос выглядит как «агент замолчал».
  shell_run "yes | '$SDKMANAGER' --sdk_root='$ANDROID_HOME' --licenses >/dev/null"
  shell_run "'$SDKMANAGER' --sdk_root='$ANDROID_HOME' 'platform-tools' 'emulator' '${SDK_PLATFORM}' '${SDK_BUILD_TOOLS}' '${SDK_SYSTEM_IMAGE}'"
fi

# ── 4. Окружение оболочки — одним размеченным блоком ─────────────────────────
step "4. ANDROID_HOME и PATH в ~/.bashrc"
BASHRC="$HOME/.bashrc"
MARK_BEGIN="# harness-mobile: начало (возможность ${ID}) — не править руками"
MARK_END="# harness-mobile: конец"
if (( DRY )); then
  printf '  + переписать блок «%s» в %s\n' "$MARK_BEGIN" "$BASHRC"
else
  touch "$BASHRC"
  python3 - "$BASHRC" "$MARK_BEGIN" "$MARK_END" "$ANDROID_HOME" "$BIN_DIR" <<'PY'
import sys, pathlib, re
rc, begin, end, android, bindir = sys.argv[1:6]
p = pathlib.Path(rc); text = p.read_text(encoding="utf-8")
block = "\n".join([
    begin,
    f'export ANDROID_HOME="{android}"',
    'export ANDROID_SDK_ROOT="$ANDROID_HOME"',
    'export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:'
    f'$ANDROID_HOME/cmdline-tools/latest/bin:{bindir}:$HOME/.maestro/bin:$PATH"',
    end, ""])
pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.S)
text = pattern.sub(block, text) if pattern.search(text) else text.rstrip("\n") + "\n\n" + block
p.write_text(text, encoding="utf-8")
print("  блок обновлён (повторная установка не размножает его)")
PY
fi

# ── 5. Скрипты петли в ~/bin ─────────────────────────────────────────────────
step "5. Скрипты петли в $BIN_DIR"
run mkdir -p "$BIN_DIR"
if (( DRY )); then
  printf '  + cp %s/bin/* %s/ && chmod +x\n' "$PACK_DIR" "$BIN_DIR"
else
  cp -f "$PACK_DIR"/bin/* "$BIN_DIR"/
  chmod +x "$BIN_DIR"/emu-* "$BIN_DIR"/mobile-loop "$BIN_DIR"/ftl-run
  printf '  поставлено: %s\n' "$(cd "$PACK_DIR/bin" && printf '%s ' *)"
fi

# ── 6. Конфиг петли — данные, собранные из паспорта ──────────────────────────
step "6. Конфиг петли $MOBILE_CONF"
if (( DRY )); then
  printf '  + собрать конфиг из паспорта, живые ключи приложения не трогать\n'
else
  mkdir -p "$(dirname "$MOBILE_CONF")"
  # Значения передаются АРГУМЕНТАМИ, а не подстановкой в текст программы:
  # FTL_DEVICES многострочен, и подставленный в литерал он рвал программу
  # пополам («unterminated string literal») — улика этой сборки.
  python3 "$PACK_DIR/sobrat-konfig.py" "$MOBILE_CONF" "$ANDROID_HOME" "$AVD_NAME" \
          "$EMULATOR_MEM_MB" "$GRADLE_MAX_MEM" "$FTL_TIMEOUT" "$FTL_DEVICES" \
          "$LOG_DIR" "$SECRETS_DIR" "$SERVICE_LOCK"
fi

# ── 7. Виртуальное устройство ────────────────────────────────────────────────
step "7. Виртуальное устройство (AVD «${AVD_NAME}»)"
if (( ! HAVE_KVM )); then
  skip "нет доступа к /dev/kvm — локальный эмулятор был бы в десятки раз медленнее" \
       "sudo usermod -aG kvm ${AGENT_USER:-$(id -un)} + перезайти; либо проверять на реальных устройствах: ftl-run"
elif [[ "$SELFTEST" == "1" ]]; then
  skip "селфтест: avdmanager не запускается" "живой прогон на сервере"
elif ! (( DRY )) && [[ ! -x "$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" ]]; then
  skip "avdmanager не установлен (шаг 2/3 не прошли)" "повторить установку"
else
  if ! (( DRY )) && "$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" list avd 2>/dev/null | grep -q "Name: ${AVD_NAME}$"; then
    printf '  уже есть: AVD «%s»\n' "$AVD_NAME"
  else
    shell_run "echo no | '$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager' create avd -n '${AVD_NAME}' -k '${SDK_SYSTEM_IMAGE}' -d '${AVD_DEVICE}' --force"
  fi
fi

# ── 8. Maestro (сценарии по экранам) ─────────────────────────────────────────
step "8. Maestro"
if [[ "$SELFTEST" == "1" ]]; then
  skip "селфтест: без сети" "живой прогон на сервере"
elif [[ -x "$HOME/.maestro/bin/maestro" ]]; then
  printf '  уже есть: %s\n' "$HOME/.maestro/bin/maestro"
else
  shell_run "curl -fsSL https://get.maestro.mobile.dev | bash" || \
    skip "установщик Maestro не прошёл" "повторить: curl -fsSL https://get.maestro.mobile.dev | bash; петля пропустит сценарии с адресом"
fi

# ── 9. EAS (сборка iOS без Mac) ──────────────────────────────────────────────
step "9. eas-cli"
if [[ "$SELFTEST" == "1" ]]; then
  skip "селфтест: без сети" "живой прогон на сервере"
elif command -v eas >/dev/null 2>&1; then
  printf '  уже есть: %s\n' "$(command -v eas)"
elif ! command -v npm >/dev/null 2>&1; then
  skip "npm не установлен — eas-cli не поставить" "node/npm приходят с первой задачей продукта на RN; повторить установку возможности после"
elif sudo -n true 2>/dev/null; then
  # Глобальный каталог npm принадлежит root: без sudo шаг падает на EACCES
  # (поймано живьём 12.08.2026). Права те же, что у шага 1, — не новое окно.
  shell_run "sudo npm install -g eas-cli" || \
    skip "sudo npm install -g eas-cli не прошёл" "смотреть /home/*/.npm/_logs/*-debug-0.log — последний по времени"
else
  shell_run "npm install -g eas-cli" || \
    skip "npm install -g eas-cli не прошёл: нет прав на $(npm config get prefix 2>/dev/null || echo 'глобальный каталог npm')" \
         "владелец выполняет одной командой: sudo npm install -g eas-cli"
fi

# ── 10. Потолок памяти сборки ────────────────────────────────────────────────
step "10. Потолок памяти Gradle (${GRADLE_MAX_MEM})"
GRADLE_PROPS="$HOME/.gradle/gradle.properties"
if (( DRY )); then
  printf '  + прописать org.gradle.jvmargs=-Xmx%s в %s\n' "$GRADLE_MAX_MEM" "$GRADLE_PROPS"
else
  mkdir -p "$(dirname "$GRADLE_PROPS")"
  touch "$GRADLE_PROPS"
  python3 - "$GRADLE_PROPS" "$GRADLE_MAX_MEM" <<'PY'
import sys, pathlib, re
p, mem = pathlib.Path(sys.argv[1]), sys.argv[2]
text = p.read_text(encoding="utf-8")
line = f"org.gradle.jvmargs=-Xmx{mem} -XX:MaxMetaspaceSize=512m"
if re.search(r"^org\.gradle\.jvmargs=.*$", text, re.M):
    text = re.sub(r"^org\.gradle\.jvmargs=.*$", line, text, flags=re.M)
else:
    text = text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n"
if not re.search(r"^org\.gradle\.daemon=", text, re.M):
    # Демон Gradle держит гигабайт между сборками — на сервере, где рядом живут
    # агент и эмулятор, это отнятая память, а не ускорение.
    text += "org.gradle.daemon=false\n"
p.write_text(text, encoding="utf-8")
print("  потолок прописан (существующая строка заменена, не продублирована)")
PY
fi

# ── 11. Живая проверка петли ─────────────────────────────────────────────────
step "11. Живая проверка: эмулятор поднимается, снимок снимается"
if (( ! HAVE_KVM )) || [[ "$SELFTEST" == "1" ]] || (( DRY )); then
  skip "локального эмулятора в этом прогоне нет" "после установки: emu-start && emu-shot проверка && emu-status && emu-stop"
else
  set +e
  PATH="$BIN_DIR:$ANDROID_HOME/platform-tools:$PATH" bash -c 'emu-start && emu-shot установка && emu-status'
  probe_rc=$?
  PATH="$BIN_DIR:$ANDROID_HOME/platform-tools:$PATH" bash -c 'emu-stop' >/dev/null 2>&1
  set -e
  if (( probe_rc )); then
    echo "  живая проверка НЕ прошла (код $probe_rc) — возможность установлена, но эмулятор не доказан."
    echo "  адрес: emu-start; tail -n 40 $LOG_DIR/mobile/emulator.log"
  else
    echo "  живая проверка прошла: устройство загрузилось, снимок снят"
  fi
fi

# ── итог ─────────────────────────────────────────────────────────────────────
STEP="итог"
cat <<EOF

=== Возможность «${ID}»: шаги установки пройдены.

ОСТАЛОСЬ РУКАМИ (физически невозможно из сессии) — подробно в ЧЕЛОВЕКУ.md:
EOF
if (( SYS_DONE )); then
  echo "  · системный шаг выполнен сам (sudo был доступен)"
else
  echo "  · системный шаг: sudo bash $SYS_SCRIPT   ← выполняет владелец"
fi
cat <<EOF
  · Firebase: проект, биллинг, сервисный аккаунт, JSON-ключ → положить в
    $SECRETS_DIR/ftl-key.json (вне репо и вебрута, И-3), затем
    gcloud auth activate-service-account --key-file="$SECRETS_DIR/ftl-key.json"
  · Expo: вход в аккаунт (eas login) — одноразово
  · Apple Developer / Google Play: регистрация, оплата, налоговые формы,
    12 живых тестеров на 14 дней — это только владелец

ДАЛЬШЕ:
  emu-status                    состояние эмулятора
  mobile-loop                   петля целиком (потребует APP_* в $MOBILE_CONF)
  bash $PACK_DIR/${CHECKLIST:-ЧЕК-ЛИСТ.md}   приёмка возможности
EOF
