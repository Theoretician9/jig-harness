# 01 — SPEC УСТАНОВКА: Claude Code на чистом Ubuntu VPS

> Вход: `00-ВВОДНЫЕ.md`, `UNIFIED/13-УСТРОЙСТВО-И-КОНФИГУРАЦИЯ.md`.
> Исполнитель: человек или агент с SSH-доступом root/sudo к серверу.
> Норма документа: **каждый шаг = команда + проверка**. Шаг без прошедшей
> проверки не считается сделанным; следующий шаг не начинается.
>
> Спека исполняется в два захода: шаги 0–7 — голая установка (до раскладки
> харнеса), шаги 8–9 и вторая половина шага 10 — **после** раскладки каталога
> `harness/` из этого пакета (этап 3). Где шаг зависит от раскладки — это
> помечено прямо в шаге.

## 0. Конфиг установки — единое место значений

Все паспортные значения живут в ОДНОМ файле; ни одно не вписывается в скрипты
руками. Создаётся первым, до всех шагов. Секретов в этом файле нет (токены —
отдельными файлами в `$SECRETS_DIR`), поэтому права 644 — его читают и агент,
и исполнитель без sudo.

```bash
sudo mkdir -p /etc/harness && sudo tee /etc/harness/install.conf >/dev/null <<'EOF'
# Паспорт установки. Заполняется при установке, см. 00-ВВОДНЫЕ.md (§5).
PROJECT_NAME=""            # имя проекта латиницей, напр. myproject
PROJECT_DIR=""             # каталог кода, напр. /opt/myproject
AGENT_USER="agent"         # системный пользователь агента
CC_VERSION=""              # версия Claude Code, фиксируется на шаге 3
SECRETS_DIR=""             # напр. /etc/myproject/secrets — ВНЕ вебрута и репо
TG_TOKEN_FILE=""           # путь к файлу с токеном бота, напр. $SECRETS_DIR/tg_bot_token
TG_CHAT_ID=""              # chat_id владельца
AUTONOMY="semi"            # semi | full — режим из 00-ВВОДНЫЕ §1.2; данные, не код
OWNER_TZ=""                # часовой пояс ВЛАДЕЛЬЦА, напр. Asia/Qostanay
HEARTBEAT_DIR="/var/lib/harness/heartbeat"
LOG_DIR="/var/log/harness"
EOF
sudo chmod 644 /etc/harness/install.conf
```

**Заполнить сразу:** `PROJECT_NAME`, `PROJECT_DIR`, `SECRETS_DIR`,
`TG_TOKEN_FILE`, `TG_CHAT_ID`, `OWNER_TZ` (дефолты и происхождение —
`00-ВВОДНЫЕ.md` §2 и §5).

`OWNER_TZ` — пояс ВЛАДЕЛЬЦА, и по нему же ставится системное время сервера:
расписания демонов считаются по времени машины, поэтому на сервере в UTC у
владельца из UTC+5 «утренняя» сводка приходила в 13:00, ночной бэкап шёл в
9 утра, а чистка диска — в 8 (улика 12.08.2026). Автоматически пояс не
подтянуть: по адресу сервера определяется пояс датацентра, а мессенджер пояс
собеседника не отдаёт — значит, это паспортное значение, как chat_id. Дальше каждый шаг начинается с гейта на нужные ему значения — пустое
значение останавливает шаг с внятным сообщением, а не даёт команде сделать
не то.

**Проверка:** `bash -n /etc/harness/install.conf && source /etc/harness/install.conf && echo OK`
— файл читается без sudo и без ошибок.

## 1. Пользователь, права, служебные каталоги

Агент живёт под отдельным пользователем, не под root: границу прав держит ОС,
а не внимание модели (главное правило проекта).

```bash
source /etc/harness/install.conf
[ -n "$PROJECT_DIR" ] || { echo "заполни PROJECT_DIR в install.conf"; exit 1; }
sudo adduser --disabled-password --gecos "" "$AGENT_USER"
sudo mkdir -p "$PROJECT_DIR" "$LOG_DIR" "$HEARTBEAT_DIR" /var/backups/harness
sudo chown -R "$AGENT_USER:$AGENT_USER" "$PROJECT_DIR" "$LOG_DIR" "$HEARTBEAT_DIR" /var/backups/harness
# sudo для агента — ТОЛЬКО поимённый список команд, не ALL:
sudo tee /etc/sudoers.d/50-agent >/dev/null <<EOF
$AGENT_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart harness-agent, /usr/bin/systemctl status harness-agent
EOF
sudo visudo -cf /etc/sudoers.d/50-agent
```

**Проверка:** `sudo -u "$AGENT_USER" whoami` → имя агента; `visudo -cf` →
«parsed OK»; `stat -c '%U' "$PROJECT_DIR" "$LOG_DIR" "$HEARTBEAT_DIR"` — везде
пользователь агента. Расширение списка sudo — только правкой этого файла с
записью причины в `ОПИСЬ.md` (право заводится под случившийся случай).

## 2. Базовые пакеты

```bash
sudo apt-get update && sudo apt-get install -y \
  git tmux curl jq python3 python3-pip python3-venv python3-yaml shellcheck ripgrep unzip
# Node.js LTS (нужен для Claude Code):
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
# Docker — при появлении продукта; на чистом харнесе не обязателен:
# curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker "$AGENT_USER"
```

**Проверка:** `git --version && tmux -V && python3 --version && node --version && jq --version`
— все команды отвечают версией; `node --version` ≥ v20.

## 3. Claude Code: версия зафиксирована

Версия выбирается один раз в момент установки и записывается в конфиг — дальше
ставится строго она; обновление версии — отдельное осознанное действие с
записью в журнал, не побочный эффект.

```bash
source /etc/harness/install.conf
V=$(npm view @anthropic-ai/claude-code version)          # актуальная на момент установки
sudo sed -i "s/^CC_VERSION=.*/CC_VERSION=\"$V\"/" /etc/harness/install.conf
sudo npm install -g "@anthropic-ai/claude-code@$V"
```

**Проверка:** `[ "$(claude --version | grep -o '[0-9][0-9.]*' | head -1)" = "$V" ] && echo OK`.

**Авторизация (Max-подписка, решение 00-ВВОДНЫЕ):** под пользователем агента,
из его домашнего каталога, выполнить `claude` и пройти вход по аккаунту —
одноразовое интерактивное действие владельца при установке, единственное
(оговорённое исключение из «интерактив выключен»; дальше интерактива нет).
⚠ Подключаться **нормальным SSH-клиентом, не браузерным терминалом** (улика
гайда владельца): браузерные терминалы ломают длинные строки при вставке —
OAuth-код приходит с переносами и даёт `OAuth error: Invalid code`.

```bash
sudo -u "$AGENT_USER" -H bash -c 'cd ~ && claude'    # вход по аккаунту, один раз
```

**Проверка:** `sudo -u "$AGENT_USER" -H bash -c 'cd ~ && claude -p "ответь одним словом: работаю" --output-format text'`
возвращает ответ модели, не ошибку авторизации.

## 4. Способ жизни процесса

Рабочая сессия агента живёт в tmux (владелец при желании может подключиться);
автозапуск после ребута держит systemd. Два правила из `UNIFIED/10`,
нарушение которых уже стоило тихих отказов: **PATH и HOME в юните задаются
явно** (у systemd куцее окружение) и у юнита есть политика перезапуска.

```bash
source /etc/harness/install.conf
[ -n "$PROJECT_DIR" ] && [ -d "$PROJECT_DIR" ] || { echo "PROJECT_DIR пуст или не создан (шаг 1)"; exit 1; }
sudo tee /etc/systemd/system/harness-agent.service >/dev/null <<EOF
[Unit]
Description=Harness agent tmux session
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=forking
User=$AGENT_USER
Environment=HOME=/home/$AGENT_USER
Environment=PATH=/usr/local/bin:/usr/bin:/bin
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/scripts/запустить-агента.sh
ExecStop=/usr/bin/tmux kill-session -t agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now harness-agent
```

`Restart=always`, а не `on-failure` (улика 11.08.2026): агент выходит с кодом 0
— и при ротации, и когда сессия завершилась сама. Для systemd это успех, и
юнит оставался лежать; сессия не возвращалась, пока её не поднимут руками.
`StartLimitIntervalSec=0` — чтобы серия быстрых падений не заблокировала
автоподъём насовсем. Ключ живёт в секции `[Unit]`, а не `[Service]`: в
`[Service]` systemd 255 его игнорирует со строкой «Unknown key name» в журнале,
и лимит молча остаётся дефолтным (замер 12.08.2026 на боевом юните:
`StartLimitIntervalUSec=10s`, `StartLimitBurst=5` вместо снятого лимита).

**Третий носитель — сторож систем `sentinel.sh`** (улика владельца 11.08.2026:
«tmux слетал полностью; надо, чтобы при любом падении и после перезагрузки
точно произошёл запуск и проверка всех систем»). Юнит поднимает сессию, но не
отвечает на вопрос «а присмотр-то бежит?»: демоны присмотра живут в cron, и
если cron умрёт, замолчат разом и сторож сессий, и утренняя сводка, которая
о нём рассказала бы. Поэтому sentinel живёт на systemd-таймере — два носителя
не отказывают одинаково. Ставится на этапе 3 (демон появляется с раскладкой):

```bash
# каждые 5 минут: юниты живы? метка сторожа сессий свежая (иначе cron не бежит)?
sudo systemctl enable --now harness-sentinel.timer
# после каждой загрузки: дождаться агента, прогнать проверки всех систем,
# прислать владельцу отчёт (агент, юниты, cron, сторожа, секреты, диск, память)
sudo systemctl enable harness-sentinel-boot.service
```

**Второй юнит — диспетчер канала** (стандартный контур, вариант Б из §7;
вечный long-poll: на нём ack ≤30 с, очередь входящих `$LOG_DIR/inbox/` и
подтверждения полуавтомата — их читают ворота выката; ставится ПОСЛЕ
раскладки харнеса — скрипт появляется на этапе 3):

```bash
sudo tee /etc/systemd/system/harness-dispatcher.service >/dev/null <<EOF
[Unit]
Description=Harness Telegram dispatcher (single getUpdates consumer)
Wants=network-online.target
After=network-online.target

[Service]
User=$AGENT_USER
Environment=HOME=/home/$AGENT_USER
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=$PROJECT_DIR/scripts/tg-dispatcher.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now harness-dispatcher
```

**Проверка (в т.ч. на больном случае):**
`systemctl is-active harness-agent` → active;
`sudo -u "$AGENT_USER" tmux ls` → сессия `agent`;
после этапа 3: `systemctl is-active harness-dispatcher` → active, и второй
экземпляр диспетчера вручную отказывается стартовать (лок);
затем тестовый `sudo reboot` — после подъёма все проверки снова зелёные,
и главная из них: `sudo -u "$AGENT_USER" tmux display-message -p -t agent
'#{pane_current_command}'` → `claude` или `node`, НЕ `bash`.

Юнит зовёт `запустить-агента.sh`, а не `tmux new-session` напрямую: голая
`tmux new-session -d -s agent` создаёт сессию с ОБОЛОЧКОЙ — systemd рапортует
`active`, tmux жив, а агента в панели нет. Улика владельца 11.08.2026: «сессия
перезагрузилась, но автозапуска не произошло». Команду запуска скрипт берёт из
`AGENT_START_CMD` (harness.conf) — тем же ключом её берёт `session-warden` при
ротации, второго источника истины нет. Демоны зовут агента отдельными
headless-вызовами (этап 3).

## 5. Секреты

Требования И-3 из `00-ВВОДНЫЕ.md`: вне репозитория, вне вебрута, права 600.

```bash
source /etc/harness/install.conf
[ -n "$SECRETS_DIR" ] || { echo "заполни SECRETS_DIR в install.conf"; exit 1; }
sudo mkdir -p "$SECRETS_DIR" && sudo chown "$AGENT_USER:$AGENT_USER" "$SECRETS_DIR" && sudo chmod 700 "$SECRETS_DIR"
# токен бота кладёт владелец (значение TG_TOKEN_FILE из конфига):
#   printf '%s' '<токен от BotFather>' | sudo -u "$AGENT_USER" tee "$TG_TOKEN_FILE" >/dev/null
#   sudo chmod 600 "$TG_TOKEN_FILE"
```

**Проверка:** `stat -c '%a %U' "$SECRETS_DIR"` → `700 agent`;
`stat -c '%a' "$TG_TOKEN_FILE"` → `600`. Проверка утечек
(`harness/scripts/check-secrets.sh`) прогоняется **после раскладки харнеса
(этап 3)**, вместе с больным случаем: файл со строкой `token = "test123"` в  <!-- не-секрет: это образец больного случая -->
репозитории → проверка обязана покраснеть, после удаления — зелёная.

## 6. Конфигурация Claude Code

Владелец читает Telegram, в терминале не присутствует (00-ВВОДНЫЕ §1.1) —
поэтому разрешения оболочки заменяются гейтами: `bypassPermissions` +
выключенный интерактив; безопасность держат хуки-сторожа и ворота — это их
норма по `UNIFIED/13`. Полуавтомат (§1.2) реализуется НЕ разрешениями
оболочки, а анонсами в канал на опасных развилках (механизм — этап 3).

```bash
source /etc/harness/install.conf
sudo -u "$AGENT_USER" mkdir -p /home/$AGENT_USER/.claude
sudo -u "$AGENT_USER" tee /home/$AGENT_USER/.claude/settings.json >/dev/null <<'EOF'
{
  "language": "russian",
  "permissions": { "defaultMode": "bypassPermissions" }
}
EOF
```

**Переменные качества рассуждений** (улики гайда владельца: деградация от
адаптивного thinking и раннего среза усилия; проверить актуальность имён
переменных для зафиксированной CC_VERSION при установке):

```bash
sudo -u "$AGENT_USER" tee -a /home/$AGENT_USER/.profile >/dev/null <<'EOF'
export CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000
export DISABLE_AUTOUPDATER=1
EOF
```

`DISABLE_AUTOUPDATER=1` — не вкусовщина, а выключение отказа, который не
чинят: версия закреплена `CC_VERSION` (§1), каталог npm принадлежит root, и
автообновление физически не может сработать — оно только красит каждый запуск
сессии сообщением об ошибке. Мигающий индикатор, который никто не пойдёт
чинить, приучает не смотреть на индикаторы вовсе; поэтому он выключается, а не
остаётся мигать. Обновлять Claude Code — отдельная задача с ревью, как всякая
смена версии.

Те же переменные добавить строками `Environment=` в оба systemd-юнита (§4) —
неинтерактивные оболочки `~/.profile` не читают. `AUTO_COMPACT_WINDOW`
согласован с ротацией: компакт наступает раньше забитого окна, а наш порог
`CTX_ROTATE_PCT`/`MAX_COMPACTS` (harness.conf) срабатывает поверх — компакт
остаётся сигналом, не нормой. Перед сложной задачей агенту доступен
`/effort max` — это записано в память агента.

Хуки — **в репозитории проекта**, не в настройках пользователя: объявление в
`$PROJECT_DIR/.claude/settings.json`, скрипты в `$PROJECT_DIR/scripts/hooks/`.
Раскладываются на этапе 3 (каталог `harness/`); путь един по всему пакету.

**Проверка (действием, а не отсутствием вопроса):**
`sudo -u "$AGENT_USER" -H bash -c 'cd ~ && claude -p "создай файл /tmp/probe_perm.txt со словом ok и скажи готово" --output-format text'`
— файл создан (`cat /tmp/probe_perm.txt` → ok) без единого запроса
разрешения; headless-вызов, упирающийся в разрешения, файла бы не создал.
После раскладки харнеса — `python3 "$PROJECT_DIR"/scripts/hooks/test_guard_bash.py`
зелёный и живой больной случай: команда из списка запретов в сессии агента →
отменена сторожем.

## 7. Канал связи: Telegram с проверкой факта доставки

Бота создаёт **владелец** (BotFather), токен — в `$TG_TOKEN_FILE` (шаг 5),
`TG_CHAT_ID` — в конфиге.

**Контур обратного направления — вариант Б (свой диспетчер), единственный
поддерживаемый на первую установку** (решение владельца 09.08.2026 по итогам
ревью): `tg-dispatcher.sh` + юнит `harness-dispatcher` (§4) несут ack ≤30 с,
очередь `$LOG_DIR/inbox/` и журнал подтверждений `confirmations.jsonl`,
который читают ворота выката (deploy_guard, режим semi).

**Вариант А (официальный telegram-плагин) — эксперимент ПОСЛЕ приёмки:**
сообщения идут прямо в сессию, но автоматического ack нет, подтверждения
пишет сам агент, и плагин забирает getUpdates (юнит диспетчера при А
выключается: `systemctl disable --now harness-dispatcher`). Включение А
оформляется задачей с ревью через полный цикл, не переключателем; до приёмки
А не включать — чек-лист раздел А при А частично красный (юнит диспетчера).

Правила ниже обязательны в обоих вариантах (отправка демонов и аварийный канал
не зависят от выбора). Правила канала (контракт для `harness/scripts/tg_send.sh`,
который появится на этапе 3; здесь проверяется голым curl):

* отправка успешна ТОЛЬКО при `ok:true` и непустом `message_id` в ответе API —
  лог «отправлено» без разбора ответа успехом не считается (норма `UNIFIED/08`);
* отказ отправки — громкий: ненулевой код выхода + запись в
  `$LOG_DIR/tg_failures.log`;
* канал допускает **одного потребителя обновлений на токен** — все чтения
  ответов владельца идут через один процесс (демон-диспетчер канала, этап 3);
  второй getUpdates-поток не поднимается никогда (ловушка из `UNIFIED/11`).

```bash
source /etc/harness/install.conf
[ -n "$TG_CHAT_ID" ] && [ -s "$TG_TOKEN_FILE" ] || { echo "заполни TG_CHAT_ID и положи токен в TG_TOKEN_FILE"; exit 1; }
TOKEN=$(sudo -u "$AGENT_USER" cat "$TG_TOKEN_FILE")
SENT_ID=$(curl -s "https://api.telegram.org/bot$TOKEN/sendMessage" \
  -d chat_id="$TG_CHAT_ID" -d text="Канал установлен. Ответьте РЕПЛАЕМ на это сообщение словом: дошло" \
  | jq -e '.result.message_id') || { echo "отправка НЕ подтверждена API"; exit 1; }
echo "message_id=$SENT_ID — ждём реплай владельца"
```

**Проверка двусторонняя:** (1) `SENT_ID` непуст — доставка подтверждена API;
(2) обратное направление — ЧЕРЕЗ ДИСПЕТЧЕР, не ручным getUpdates (диспетчер —
единственный потребитель getUpdates; ручной вызов украдёт апдейт и вгонит юнит
в 409-паузу): после `systemctl start harness-dispatcher` владелец отвечает
«дошло», и в течение минуты появляется файл входящего:
`ls -t "$LOG_DIR/inbox/" | head -1` → `cat` показывает «дошло». Обе половины
обязательны: канал, в котором агент пишет, но не слышит, в полуавтомате
бесполезен. (До включения юнита обратное направление проверить нечем — это
нормально: пункт закрывается на шаге 9/приёмке.)

## 7-а. Доставка и раскладка харнеса

Скопировать на сервер (scp/rsync) два каталога РЯДОМ: `STARTER-PACKAGE/` и
`UNIFIED/` (например, в `/home/$AGENT_USER/starter/`), затем:

```bash
sudo -u "$AGENT_USER" bash /home/$AGENT_USER/starter/STARTER-PACKAGE/harness/razlozhit-harnes.sh
```

Раскладчик кладёт хуки в `$PROJECT_DIR/scripts/hooks/`, скрипты в `scripts/`,
демоны в `harness/demons/`, скилы в `harness/skills/`, доставляет `UNIFIED/` и
не затирает живые данные при повторном запуске.
**Проверка:** `razlozhit-harnes.sh --selftest` зелёный; вывод раскладки
содержит «UNIFIED доставлен»; после `git init` повторный запуск подключает
pre-commit симлинком.

## 8. Расширения

Ставится сразу: канал (§7). Всё остальное — по условию, механизмом:
скил `установка-расширений` (`harness/skills/установка-расширений/`) содержит
таблицу «условие → команды» и список запрещённых с причинами; агент применяет
его сам при наступлении условия, по одному, с `/reload-plugins`, записью цели
и перемером через месяц. Дополнение таблицы — правка
`harness/skills-src/установка-расширений.md` + перегенерация скилов.

**Проверка (после этапа 3):** скил существует; журнал вызовов скилов заведён;
установленное на сервере не выходит за «канал + применённые по условию строки
таблицы».

## 8-а. Возможности (крупные наборы)

Набор, которому нужны системные пакеты, скрипты в `~/bin`, свои скилы, свои
запреты сторожу и свой реестр действий владельца, строкой таблицы не
описывается. Такой набор — каталог с паспортом в `harness/vozmozhnosti/<id>/`:

| файл | обязателен | что несёт |
|---|---|---|
| `МАНИФЕСТ.conf` | да | паспорт: `ID`, `CONDITION`, требования, пороги. Ключи ЛАТИНИЦЕЙ (оболочка не присваивает кириллические имена), формат как у `harness.conf` |
| `установить.sh` | да | шаги установки; `--dry-run`, `--selftest`, идемпотентность |
| `skills-src/*.md` | нет | источники скилов; собираются ТОЛЬКО у установленного набора |
| `память/*.md` | нет | записи в память + строки указателя и таблицы срабатываний метками `<!-- указатель: -->` / `<!-- таблица: -->` |
| `bin/*` | нет | скрипты в `~/bin` |
| `запреты.list` | нет | строки сторожу `guard_bash`: `РЕГЭКСП<TAB>СООБЩЕНИЕ` |
| `уборка.list` | нет | строки демону `disk-cleanup`: `КАТАЛОГ<TAB>ДНЕЙ` |
| `гейты/*.py` | нет | гейты `pre-commit` (список staged приходит на stdin) |
| `gitignore.list` | нет | строки в `.gitignore` проекта |
| `ЧЕЛОВЕКУ.md` | нет | что физически не может агент — уходит владельцу вложением |
| `ЧЕК-ЛИСТ.md` | нет | приёмка набора, включая больные случаи |

Единственная точка установки — `scripts/vozmozhnost.sh`:

```bash
bash scripts/vozmozhnost.sh список                    # что есть, при каком условии
bash scripts/vozmozhnost.sh требования <id>           # замер ВЕЩЕЙ до установки
bash scripts/vozmozhnost.sh поставить  <id> [--dry-run]
bash scripts/vozmozhnost.sh состояние  [<id>]
bash scripts/vozmozhnost.sh снять      <id>
```

Метка `.установлено` в каталоге набора — водораздел механизма: до неё скилов
набора нет вовсе, его запреты сторож не читает, его каталоги демон не чистит,
его гейты не запускаются. Поэтому «поставить, когда пригодится» здесь механизм,
а не намерение: непоставленный набор не занимает ни строки контекста.

Возможность **не добавляет строк в crontab** намеренно: набор демонов сверяется
с таблицей §9 при установке, и лишняя строка считалась бы расхождением.
Периодическая работа набора идёт данными существующих демонов (`уборка.list`).

В пакете есть одна возможность — `мобильная-разработка` (React Native + Expo,
Android локально, iOS через EAS). Ставится не при установке харнеса, а когда в
задаче продукта появилось приложение.

**Проверка (без сервера):** `bash harness/scripts/vozmozhnost.sh --selftest`
зелёный; `bash harness/vozmozhnosti/mobilnaya-razrabotka/ustanovit.sh --selftest`
зелёный; `python3 harness/hooks/test_guard_bash.py` зелёный, включая путь
«набор не установлен → его запреты молчат».
**Проверка (на сервере):** `vozmozhnost.sh список` показывает набор как «не
стоит»; `harness/skills/` не содержит `mobile-*` до установки.

## 9. Расписание демонов и heartbeat — после раскладки харнеса (этап 3)

До раскладки `harness/demons/` этот шаг **пропускается**: cron на
несуществующие скрипты — тихий отказ при каждом запуске. Демоны — на
системном cron (переживает перезапуск оболочки), от имени `$AGENT_USER`;
каждый пишет свой лог в `$LOG_DIR/<имя>.log` и обновляет метку
`$HEARTBEAT_DIR/<имя>` при КАЖДОМ успешном прогоне.

```bash
source /etc/harness/install.conf
D="$PROJECT_DIR/harness/demons"
[ -d "$D" ] || { echo "харнес ещё не разложен (этап 3) — шаг пропустить"; exit 1; }
sudo crontab -u "$AGENT_USER" - <<EOF
*/10 * * * *  $D/session-warden.sh        >>$LOG_DIR/session-warden.log 2>&1
0 * * * *     $D/task-closer.sh           >>$LOG_DIR/task-closer.log 2>&1
0 3 * * *     $D/disk-cleanup.sh          >>$LOG_DIR/disk-cleanup.log 2>&1
30 3 * * *    $D/server-hygiene.sh        >>$LOG_DIR/server-hygiene.log 2>&1
0 4 * * *     $D/backup.sh                >>$LOG_DIR/backup.log 2>&1
15 4 * * *    $D/devmap-selfheal.sh       >>$LOG_DIR/devmap-selfheal.log 2>&1
5 * * * *     $D/evo-collector.sh         >>$LOG_DIR/evo-collector.log 2>&1
10 * * * *    $D/tokens-collector.sh      >>$LOG_DIR/tokens-collector.log 2>&1
0 8 * * *     $D/heartbeat-watch.sh       >>$LOG_DIR/heartbeat-watch.log 2>&1
0 9 1 * *     $D/memory-revision.sh       >>$LOG_DIR/memory-revision.log 2>&1
EOF
```

**Heartbeat — страховку нельзя оценивать по частоте обращений к ней:**

* метка обновляется только при успешном завершении демона;
* `heartbeat-watch.sh` раз в сутки сверяет возраст каждой метки с её
  периодом ×2 и шлёт в канал сводку. **Сводка отправляется всегда, даже когда
  всё зелёное** («все демоны живы») — молчание сторожа неотличимо от его
  смерти, поэтому смерть самого сторожа владелец обнаруживает по ОТСУТСТВИЮ
  утреннего сообщения (норма `UNIFIED/08`);
* каждый демон при установке прогоняется в чистом окружении — как его увидит
  cron, а не оболочка исполнителя (ловушка «своя оболочка прячет дефект»),
  образец:
  `env -i HOME=/home/$AGENT_USER PATH=/usr/local/bin:/usr/bin:/bin /bin/sh -c "$D/heartbeat-watch.sh"; echo exit=$?`
  — выход 0 и свежая метка в `$HEARTBEAT_DIR`.

**Проверка:** `crontab -l -u "$AGENT_USER"` совпадает с таблицей; каждый демон
прогнан образцом выше с exit=0; после первых суток `ls -l "$HEARTBEAT_DIR"` —
все метки свежее своих периодов; больной случай — закомментировать строку
одного демона → завтрашняя сводка обязана назвать его по имени.

## 10. Финальная проверка установки

**Сразу после шагов 0–7:**

1. `systemctl is-active harness-agent` → active (и после тестового ребута);
2. `claude --version` = `CC_VERSION`; headless-вызов отвечает; проба записи
   файла из шага 6 проходит без запроса разрешений;
3. канал: отправка `ok:true` + реплай владельца «дошло» прочитан по
   `reply_to_message`.

**После раскладки харнеса (этап 3):**

4. `crontab -l` = таблица шага 9; прогоны в `env -i` зелёные; метки heartbeat
   появляются; больной случай heartbeat назван в сводке;
5. `check-secrets.sh` по репо пуст; больной случай краснеет;
6. `test_guard_bash.py` зелёный; живой запрет отменён сторожем;
7. журнал вызовов скилов/гейтов существует и пишется (пустой — нормально,
   отсутствующий — нет).

Дальше — `03-ЗАДАНИЕ-НОВОМУ-АГЕНТУ.md` (первые сессии агента) и
`ЧЕК-ЛИСТ-ПРИЁМКИ.md` (приёмка владельцем).
