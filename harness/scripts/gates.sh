#!/usr/bin/env bash
#
# gates.sh — все гейты харнеса одним вызовом.
#
# Зачем. Цена шага агента — весь накопленный контекст: замер 12.08.2026 дал
# 158 535 токенов в среднем на шаг. Пять гейтов, прогнанные по одному, стоят
# впятеро дороже одного шага и ничего не добавляют к результату. Замер того же
# дня: 1081 шаг с инструментами, из них с одним вызовом — 1081, с двумя и
# более — ноль. Правило «собирай независимые проверки в один шаг» есть в
# системном промпте и дало 0 из 1081 — значит дробить должно быть НЕЧЕГО, а не
# «не рекомендуется». Этот скрипт и есть один шаг вместо пяти.
#
# Вывод скуп нарочно: зелёная проверка — одна строка, красная — полный вывод.
# Прогон, вываливающий двести строк успеха, съедает ровно то, что экономит.
#
# Запуск:
#   gates.sh            # гейты: секреты, гигиена, сторож команд, пакет, карта
#   gates.sh --всё      # то же + самотесты демонов и скриптов (долго)
#   gates.sh --selftest # проверка самих ворот на больном случае
#   gates.sh --хакатон  # только быстрое подмножество из harness/config/gates-hackathon.yaml
#
# Код выхода = число красных проверок (0 = чисто).
set -uo pipefail
export LC_ALL=C.UTF-8

# Место журнала — от КОРНЯ установки, общим загрузчиком: зашитый
# «/var/log/harness» ставил ворота ровно на одну машину. Имя переменной
# латиницей: кириллические имена bash не берёт ([[cyrillic-in-bash-names]]).
# shellcheck disable=SC1091
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/config.sh"
konf_zagruzit
ITOG_LOG="$LOG_DIR/ворота-итог.jsonl"
REZHIM="обычный"   # «всё» ставится ниже, у флага --всё

# Голый mktemp в пробах кладёт tmp.XXXX прямо в /tmp, а уборка /tmp такое не опознаёт и держит
# гигабайты неделями. Все пробы ворот наследуют этот каталог с известным префиксом.
RUN_TMP=$(mktemp -d -t harness-run-XXXXXX)
export TMPDIR="$RUN_TMP"
trap 'rm -rf "$RUN_TMP"' EXIT

# Обёртка проверки и счётчики зелёных/красных/жёлтых — scripts/lib/gates_check.sh.
# shellcheck disable=SC1091
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/gates_check.sh"

# ── самотест: зелёная и красная проверки обязаны считаться порознь ───────────
if [ "${1:-}" = "--selftest" ]; then
    # Вывод — в файл, а не в $(...): подстановка запускает подоболочку, и
    # счётчики выросли бы в ней, а здесь остались нулями. Тогда самотест
    # хвалил бы ворота за работу, которой не было.
    TRACE=$(mktemp)
    # Журнал секунд самотеста — во временный каталог: боевой журнал не
    # засоряется строками фиктивных проверок.
    GATES_TIMING_DIR=$(mktemp -d)
    trap 'rm -f "$TRACE"; rm -rf "$GATES_TIMING_DIR" "$RUN_TMP"' EXIT

    # БОЛЬНОЙ СЛУЧАЙ первым: падающая проверка обязана попасть в красные И
    # показать свой вывод. Ворота, которые глотают падение, хуже отсутствия
    # ворот — они говорят «чисто» там, где не чисто.
    проверка "падающая" bash -c 'echo "вот что сломалось"; exit 3' > "$TRACE" 2>&1
    [ "$RED" = 1 ] || { echo "САМОТЕСТ ПРОВАЛЕН: падение не посчитано красным"; exit 1; }
    grep -q 'вот что сломалось' "$TRACE" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: вывод упавшей проверки не показан"; cat "$TRACE"; exit 1; }
    grep -q 'код 3' "$TRACE" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: код возврата упавшей проверки не назван"; cat "$TRACE"; exit 1; }

    проверка "проходящая" bash -c 'echo "первая строка"; echo "последняя строка"' > "$TRACE" 2>&1
    [ "$GREEN" = 1 ] || { echo "САМОТЕСТ ПРОВАЛЕН: успех не посчитан зелёным"; exit 1; }
    grep -q 'последняя строка' "$TRACE" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: у зелёной не показана последняя строка"; cat "$TRACE"; exit 1; }
    if grep -q 'первая строка' "$TRACE"; then
        echo "САМОТЕСТ ПРОВАЛЕН: зелёная вывалила всё, а должна одну строку"; cat "$TRACE"; exit 1
    fi
    # Жёлтый: находки показываются ЦЕЛИКОМ, но в красные не идут.
    проверка "жёлтая" bash -c 'echo "находка обкатки"; exit 77' > "$TRACE" 2>&1
    [ "$RED" = 1 ] || { echo "САМОТЕСТ ПРОВАЛЕН: жёлтая посчиталась красной"; exit 1; }
    [ "$YELLOW" = 1 ] || { echo "САМОТЕСТ ПРОВАЛЕН: жёлтая не посчитана"; exit 1; }
    grep -q 'находка обкатки' "$TRACE" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: находка жёлтой не показана"; cat "$TRACE"; exit 1; }

    # Секунды каждой проверки — по строке на проверку в журнале прогона.
    [ "$(grep -c '"event": "gate.check"' "$GATES_TIMING_DIR/gates-timing.jsonl" 2>/dev/null)" = 3 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: журнал секунд — не по строке на каждую из 3 проверок"; exit 1; }

    echo "САМОТЕСТ ПРОЙДЕН: 7 путей (падение красное с выводом и кодом, успех зелёный одной строкой, жёлтая отдельно и целиком, журнал секунд)"
    exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || { echo "не нашёл корень проекта"; exit 1; }

# Выборка проверок по имени (через «|») — для живой пробы прогона по снимку,
# которому полный набор стоит полчаса. Аргумент, а не переменная среды:
# унаследованная переменная тихо урезала бы ворота выката.
ONLY_CHECKS=""
if [ "${1:-}" = "--только" ]; then
    ONLY_CHECKS="${2:?--только: нужен список имён проверок через |}"
    shift 2
    echo "--- выборка: $ONLY_CHECKS ---"
fi

echo "=== ворота харнеса: $(date '+%d.%m %H:%M') ==="

# Быстрый прогон режима хакатон: состав — данные (gates-hackathon.yaml), а не
# вторая копия списка в коде; механизм тот же «проверка». Полный набор ниже не идёт.
if [ "${1:-}" = "--хакатон" ]; then
    REZHIM="хакатон"
    export ROOT
    # Список читается ДО прогона: нечитаемый или пустой состав — отказ, а не
    # «ноль красных»: пустые быстрые ворота пускали бы выкат без единой проверки.
    HK_LIST=$(mktemp)
    if ! python3 -c '
import sys, yaml
checks = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["checks"]
if not checks:
    sys.exit("список checks пуст")
for item in checks:
    sys.stdout.write(item["name"] + "\0" + item["cmd"] + "\0")' \
            "$ROOT/harness/config/gates-hackathon.yaml" > "$HK_LIST"; then
        rm -f "$HK_LIST"
        echo "ворота хакатон: список проверок gates-hackathon.yaml пуст или нечитаем — отказ"
        exit 1
    fi
    while IFS= read -r -d '' NAME && IFS= read -r -d '' CMD; do
        проверка "$NAME" bash -c "$CMD"
    done < "$HK_LIST"
    rm -f "$HK_LIST"
    echo "=== итог: зелёных $GREEN, красных $RED ==="
    printf '{"ts":"%s","режим":"%s","зелёных":%s,"красных":%s}\n' \
        "$(date -Is)" "$REZHIM" "$GREEN" "$RED" >> "$ITOG_LOG" 2>/dev/null || true
    exit "$RED"
fi
# Производные документы на свежей установке не лежат: раскладчик их не кладёт,
# собирает их только update.py. Без этого первые же ночные ворота краснеют на
# «документ разошёлся с реестром», хотя расходиться нечему — документа просто
# нет. Собираем ТОЛЬКО отсутствующий: существующий, правленный руками, должен
# остаться красным.
for sborka in "build-registry-doc.py:docs/УСТРОЙСТВО-ХАРНЕСА.md" \
              "build-live-check-list.py:docs/живая-проверка-механизмов.md"; do
    [ -e "$ROOT/${sborka#*:}" ] || python3 "scripts/${sborka%%:*}" >/dev/null 2>&1 \
        || echo "первая сборка ${sborka#*:} не удалась — гейт ниже покажет причину"
done
# Корень — аргументом: без него гейт берёт PROJECT_DIR паспорта и в прогоне по
# снимку судил бы живое дерево.
проверка "секреты"        bash scripts/check-secrets.sh "$ROOT"
# Права ВНУТРИ каталога секретов не проверял никто: два файла пролежали
# читаемыми для всей машины с 10.09 по 27.09.2026 (замер ревизии харнеса).
проверка "права секретов" python3 scripts/check-secrets-perms.py
проверка "гигиена"        python3 scripts/check-context-hygiene.py --repo "$ROOT"
# Карта едет в контекст КАЖДОЙ сессии: подробности карточки нужны при разборе
# задачи, а не всегда. Замер 12.09.2026 — 31 карточка держала 29 397 лишних
# знаков, карта была на 92 % потолка.
проверка "прополка карты" python3 scripts/weed-the-map.py --проверить
проверка "прополка-самотест" python3 scripts/weed-the-map.py --selftest
# Карту пишут пятеро, и общий лок брали двое: атомарная запись спасает от
# полуфайла, но не от гонки — пока демон читал карту, агент дописал карточку,
# и замена стёрла её (замер 12.09.2026).
проверка "писатели карты" python3 scripts/check-map-writers.py
проверка "писатели-самотест" python3 scripts/check-map-writers.py --selftest
# Механика очереди канала — общая у обоих сторожей; её больные случаи
# (курсор отстал, первый запуск, пометка второй раз) живут в самотесте
# модуля, а не в каждом стороже по копии.
проверка "чтение настроек" python3 scripts/lib/config.py --selftest
проверка "дверь укладки" python3 scripts/test_layout_door.py
проверка "опись раскладки" python3 scripts/test_layout_manifest.py
проверка "журнал раскладки" python3 scripts/test_layout_journal.py
проверка "история раскладки" python3 scripts/test_layout_history.py
проверка "замок раскладки" python3 scripts/test_layout_lock.py
проверка "заметки выпуска" python3 scripts/test_release_notes.py
# Только этот самотест судит plan_absent, info/exclude, коммит пакета и связку
# «дверь ↔ раскладчик»; раскладчик живёт в пакете, а не в проекте.
проверка "самотест раскладчика" bash -c 'bash "$(python3 -c "import sys; sys.path.insert(0, \"scripts/lib\"); import config; print(config.katalog_paketa())")/harness/razlozhit-harnes.sh" --selftest'
# Стенд самотеста весит гигабайты: обрыв по timeout обязан его убирать.
проверка "самотест раскладчика: уборка при обрыве" python3 scripts/test_layout_selftest_cleanup.py
# Три несущих дефекта выпуска 0.9.3: каждый проходил ворота пакета зелёным и ронял установку с нуля.
проверка "самотест раскладчика: код выхода" python3 scripts/test_layout_selftest_exit_code.py
проверка "выгрузка UNIFIED свежа" python3 scripts/test_unified_fresh.py
проверка "установщик: отказ первого коммита" python3 scripts/test_installer_first_commit.py
проверка "раскладка печатает эталон личности" python3 scripts/test_layout_self_seal.py
# Четыре несущих дефекта живой установки R7 (Д-20…Д-23): каждый ронял код установки или эталон.
проверка "установщик: эталон на обновлении" bash scripts/test_installer_resume_seal.sh
проверка "установщик: шаг 13 до входа" bash scripts/test_installer_demon_probe.sh
проверка "первый коммит установщика не мимо карты" bash scripts/test_devmap_selfheal_installer_commit.sh
проверка "id сессии демона без uuidgen" bash scripts/test_claude_demon_session_id.sh
# Голый mktemp проб обязан попадать в каталог harness-run-, который уборка /tmp опознаёт.
проверка "каталог проб ворот" python3 scripts/test_gates_run_temp_dir.py
проверка "границы задачи" python3 scripts/lib/cards.py --selftest
# Четыре пробы, которые не звал НИКТО: их нашёл гейт «кто зовёт» 14.09.2026 —
# ровно тот случай, ради которого он написан (проверка есть, вызова нет).
проверка "кто на порту"   python3 scripts/test_who_is_on_port.py
проверка "права наружу"   bash scripts/test_panel_root.sh
проверка "установка docker" bash scripts/test_install_docker.sh
проверка "шаблоны юнитов установщика" bash scripts/test_installer_units.sh
проверка "пути установщика на обновлении" bash scripts/test_installer_paths.sh
проверка "хук копит провалы" bash scripts/test_precommit_accumulates.sh
проверка "контейнеры в отчёте старта" bash scripts/test_session_state_containers.sh
проверка "первая задача: путь продукта" bash scripts/test_first_task_product_path.sh
проверка "известный отказ хоста" bash scripts/test_session_known_failed.sh
проверка "повтор квитанции" bash scripts/test_ack_retry.sh
проверка "глубина автоматом" python3 scripts/test_depth_automatic.py
проверка "задание и метка" python3 scripts/check-job-and-mark.py
проверка "метка-самотест" python3 scripts/check-job-and-mark.py --selftest
проверка "сборка system-jobs" python3 scripts/build-system-jobs.py --check
# Библиотека процессов ↔ демоны, хуки, крон, юниты systemd (живая машина тоже).
проверка "библиотека процессов" python3 scripts/check-process-library.py
проверка "реестр стека" python3 scripts/check-stack-registry.py
проверка "реестр стека-самотест" python3 scripts/check-stack-registry.py --selftest
# Личность и зов модели (hr.standards-and-policy): пробы носителей, стенд
# каждой подменяет claude, модель не тратится. Живая приёмка — только с
# ключом --подставной; живьём её гонит смена по слову.
проверка "личность: сборка"           python3 scripts/test_self_prompt.py
проверка "личность: носитель claude-run" bash scripts/test_claude_run.sh
проверка "аккаунт: переключение смены" bash scripts/test_switch_account.sh
проверка "личность: карточки помощников" python3 scripts/test_agent_cards.py
проверка "личность: печать эталона"   bash scripts/test_self_seal.sh
проверка "личность: слой человека"    python3 scripts/test_self_set.py
проверка "личность: слои памяти, хук" python3 scripts/hooks/test_memory_layer_guard.py
проверка "личность: прополка памяти"  python3 scripts/test_memory_weeding.py
проверка "личность: сборка без самоимпорта" bash scripts/test_layout_no_self_import.sh
проверка "личность: ключ события"     bash scripts/test_live_key.sh
проверка "личность: смена без личности" bash scripts/test_shift_self_mark.sh
проверка "личность: носитель сессии"  bash scripts/test_session_carrier.sh
проверка "личность: живая приёмка (подставной claude)" bash scripts/test_self_prompt_live.sh --подставной
проверка "зов модели: сторож"         python3 scripts/test_check_model_call.py
проверка "зов модели: приборы"        python3 scripts/test_instrument_carrier.py
проверка "зов модели: код 79 у фоновых служб" bash scripts/test_carrier_exit79.sh
проверка "зов модели: код 79 у входа" python3 scripts/test_login_expiry_exit79.py
проверка "кто зовёт"      python3 scripts/check-who-calls.py
# Мера Ф2 ревизии: население — ВСЕ .py/.sh харнеса (193), а не только
# механизмы реестра. Печатает СПИСОК: инструменты смены, живых только через
# свою пробу и ничьих. Ничей файл — красный.
проверка "население кода" python3 scripts/check-who-calls.py --население
# §4.3 ревизии: одна форма — один судья. Пять форм жили двумя копиями, а слова
# отказа владельца — тремя (третья в tg-dispatcher.sh, точке приёма его
# сообщений). Копия выглядит согласованной, пока кто-нибудь не поправит одну.
проверка "один судья" python3 scripts/check-one-judge.py
проверка "судья-самотест" python3 scripts/check-one-judge.py --selftest
проверка "формы значений" python3 scripts/lib/patterns.py --selftest
# Мера Ф3 ревизии: считаем УСПЕШНЫЕ машинные вызовы скилов, а не запуски —
# прогон с отказом ставит запись и выглядит работой. Потолок молчащих —
# ключ SKILL_SILENT_MAX в harness.conf, он уменьшается по мере выбора исходов.
проверка "вызовы скилов" python3 scripts/check-skill-usage.py
проверка "скилы-самотест" python3 scripts/check-skill-usage.py --selftest
проверка "скил перед агентом" python3 scripts/hooks/agent_skill_guard.py --selftest
# Ф4 ревизии, класс 1: проба, грепающая свой исходник, зеленеет от слова в
# комментарии. Законные случаи (страж регресса, подсчёт своих путей, боевое
# значение порога) объявлены с причиной в harness/config/own-text-reads.yaml.
проверка "свой текст" python3 scripts/check-own-text-probes.py
проверка "свой текст-самотест" python3 scripts/check-own-text-probes.py --selftest
проверка "кто-зовёт-сам"  python3 scripts/check-who-calls.py --selftest
проверка "очередь канала" python3 scripts/lib/channel_queue.py --selftest
проверка "указания"       python3 scripts/orders.py
# Строка приказов не уходит из ORDERS и слов владельца без переноса в архив прополки:
# в рабочем дереве потерю не видно, видна только против HEAD~.
проверка "прополка без потерь" python3 scripts/check-memory-weeding.py
# Задача, сказанная в канал, обязана дойти до карты: 941 сообщение в очереди,
# 171 с признаком работы, и связи «сообщение → задача» не было ни одной.
проверка "задачи из канала" python3 scripts/tasks-from-channel.py
# Сама приёмка проверок не делает ворота красными — она про содержание работы,
# а не про здоровье машины. В воротах стоит её САМОТЕСТ: механизм, судящий о
# доказанности, обязан быть доказан сам.
проверка "приёмка-самотест" python3 scripts/acceptance.py --selftest
# Ускорение мутаций не смеет превратить «нечем проверить» в «всё поймано».
проверка "ускорение мутаций" python3 scripts/test_mutation_speed.py
# Оболочка ночному агенту закрыта не списком инструментов, а режимом прав:
# живая проба 11.09.2026 показала, что --allowedTools настройки проекта
# перекрывают. Гейт следит, чтобы флаг стоял в КАЖДОМ вызове агента.
проверка "замок агента"   python3 scripts/agent-lock-check.py
проверка "замок-самотест" python3 scripts/agent-lock-check.py --selftest
проверка "сторож команд"  python3 scripts/hooks/test_guard_bash.py
проверка "ворота выката"  python3 scripts/test_deploy_guard.py
проверка "свои процессы"  python3 scripts/test_deploy_guard_own_user.py
проверка "два агента"     python3 scripts/test_deploy_guard_two_agents.py
проверка "процессы агента"  python3 scripts/test_agent_processes.py
проверка "одна смена"       python3 scripts/test_one_shift.py
проверка "потеря сокета"    bash scripts/test_warden_socket_loss.sh
проверка "изоляция tmux"    python3 scripts/check-tmux-isolation.py
проверка "изоляция-самотест" python3 scripts/check-tmux-isolation.py --selftest
проверка "уборка не гасит окно" bash scripts/test_tmux_isolation.sh
проверка "дверь данных"   python3 scripts/test_data_delete_door.py
проверка "слово у двери удаления" python3 scripts/test_protected_delete_word.py
проверка "живая проверка" python3 scripts/build-live-check-list.py --проверить
проверка "живая проверка в pre-commit" bash scripts/test_precommit_live_check.sh
# Список файлов сверке подаётся ЯВНО. БОЛЬНОЙ СЛУЧАЙ 12.08.2026: без него
# скрипт читал stdin ворот, а тот в сессии агента открыт и молчит — ворота
# висели одиннадцать минут и были сняты вручную, ни одной проверки не показав.
# Пустой индекс — законный ответ «сверять нечего»: настоящую сверку делает
# pre-commit, у которого список staged-файлов есть всегда.
проверка "сверка пакета"  bash -c 'git -c core.quotepath=false diff --cached --name-only | python3 scripts/compare-with-package.py'
# Что обходить не надо — из конфига (GATES_SKIP_PATHS): каталог продукта,
# вшитый в ворота харнеса, на чужом проекте не исключил бы ничего, зато
# гейт полез бы в чужие зависимости (разбор кода 11.09.2026).
SKIP_ARGS=""
for SKIP_PATH in ${GATES_SKIP_PATHS:-./.git/*}; do
    SKIP_ARGS="$SKIP_ARGS -not -path $SKIP_PATH"
done
проверка "кириллица"      bash -c 'find . -name "*.sh" '"$SKIP_ARGS"' -print0 | xargs -0 python3 scripts/check-cyrillic-in-names.py'
проверка "секрет в логах" python3 scripts/check-secret-in-logs.py
проверка "секрет в логах: ключи" python3 scripts/test_secret_in_logs_keys.py
# Журналы харнеса: писатели и демоны (G1), реестр ↔ код (G2), диск → реестр (G2),
# потеря без разбора и тишина при работе сессии (G3).
проверка "журналы: писатели"   python3 scripts/check-event-writers.py
проверка "журналы: реестр"     python3 scripts/check-event-writers.py --registry
проверка "журналы: диск"       python3 scripts/check-event-writers.py --disk
проверка "журналы: потери"     python3 scripts/events.py lost --gate
# Имена НОВЫХ файлов кода — латиницей (задача владельца 11.09). Существующие
# 601 переводятся отдельной задачей: их переименование трогает прод.
проверка "имена файлов"   bash -c 'git -c core.quotepath=false diff --cached --name-only --diff-filter=AR | python3 scripts/check-file-names.py'
# Присваивание из конвейера с grep под set -e: пустой grep роняет ВЕСЬ
# скрипт молча. Так умирал сторож сессий, когда fuser не показывал
# держателя лока — ни одной строки в лог, ротации нет (улика 11.09.2026).
проверка "падение под set -e" python3 scripts/check-failure-under-set-e.py
# Середина пайплайна: спека и её ревью у крупной задачи (первый пункт владельца
# 11.09). Уровень задачи — из замера индекса, а не из заявленного объёма.
проверка "пайплайн-гейт"  python3 scripts/check-pipeline.py
# Дубль той же проверки ПО ИСТОРИИ задачи: pre-commit обходится одним
# `--no-verify`, и у пайплайна, в отличие от секретов, дубля не было вовсе
# (ревью спеки 23.09.2026). Здесь работа судится целиком по трейлерам
# «Dev-Map», то есть видна и та, что прошла мимо ворот.
проверка "пайплайн-история" python3 scripts/check-pipeline.py --по-истории
проверка "тесты до кода"  python3 scripts/check-tests-first.py
# Импорт ведёт в существующий модуль. Перевод имён 11.09 оставил 171 импорт
# указывать на исчезнувшее имя, и НИ ОДИН сторож этого не увидел: ruff судит
# имена, а не модули; тесты по правилу гоняются только перед выкатом.
проверка "импорты"        python3 scripts/check-imports.py
проверка "импорты-самотест" python3 scripts/check-imports.py --selftest
# Конфиг читают общим загрузчиком: голый source делает файл старше окружения,
# и любая проба играет на боевом (улика 12.09.2026 — смена модели в рабочей
# панели). Гейт судит всё дерево, самотест — девять путей.
проверка "загрузка конфига" python3 scripts/check-config-loading.py
проверка "загрузка-самотест" python3 scripts/check-config-loading.py --selftest
проверка "загрузчик конфига" bash scripts/lib/config.sh --selftest
проверка "корень установки" python3 scripts/check-install-root.py
проверка "метка-лимита"   python3 scripts/limit_mark.py --selftest
проверка "искатели-глав"  python3 scripts/check-unified-seekers.py
проверка "семена"         python3 scripts/check-seed-templates.py
проверка "семена-самотест" python3 scripts/check-seed-templates.py --selftest
проверка "искатели-самотест" python3 scripts/check-unified-seekers.py --selftest
проверка "сторож паузы" python3 scripts/hooks/pause_guard.py --selftest
проверка "корень-самотест" python3 scripts/check-install-root.py --selftest
# Функция оболочки, званная выше своего определения, печатает одну строку в
# stderr и возвращает ПУСТО: отказ выглядит как «данных нет». Так в стартовом
# отчёте месяц было пустым поле «последний коммит» (улика 12.09.2026).
проверка "функция до определения" python3 scripts/check-function-before-definition.py
проверка "функция-самотест" python3 scripts/check-function-before-definition.py --selftest
# Расписания и юниты зовут файлы проекта, а живут в /etc — репозиторные гейты
# их не видят. 11.09 переименование scripts/fonovaya-cel.sh оставило пять
# ночных целей продукта в cron звать исчезнувшее имя.
# Внешняя поверхность = слушатели ПЛЮС прокси: панель слушает 127.0.0.1:8787, и
# мера «кто слушает 0.0.0.0» её не видела, хотя nginx отдаёт её в мир. Плюс
# разбор Host: без default_server на порту отвечает первый блок по алфавиту.
# Объявленное наружу задаётся ключом EXTERNAL_SURFACE_DECLARED — данными.
проверка "внешняя поверхность" python3 scripts/check-external-surface.py
проверка "поверхность-самотест" python3 scripts/check-external-surface.py --selftest
проверка "внешние вызовы" python3 scripts/check-external-callers.py
# Ручки панели: каждая что-то делает и делает, когда обещано. Улики ревью
# спеки панели 11.09: ручка, которую никто не читает, и ручка, обещающая
# «сразу» при чтении раз в десять минут, — обман владельца, невидимый глазами.
проверка "прополка указаний" python3 scripts/weed-the-orders.py --selftest
проверка "тон панели"     python3 scripts/check-panel-tone.py
проверка "тон-самотест"   python3 scripts/check-panel-tone.py --selftest
проверка "переключатели"  python3 scripts/check-switches.py
проверка "переключатели-самотест" python3 scripts/check-switches.py --selftest
# Механика записи с панели (копия, права, хозяин, подъём через sudo, атомарная
# подмена) — общая у трёх писателей; её больные случаи живут здесь.
проверка "писатель панели" python3 scripts/lib/panel_writer.py --selftest
# Единственный писатель настроек: узкий вход вместо прав root у панели.
проверка "писатель настроек" python3 scripts/test_write_key.py
# Служба панели: вход через канал и защита от чужих запросов.
проверка "вход панели"    python3 harness/panel/test_login.py
проверка "служба панели"  python3 harness/panel/test_server.py
проверка "подтверждение панели" python3 harness/panel/test_confirmation.py
проверка "витрина панели"  python3 harness/panel/test_state.py
# Канбан: снимок доски, её страницы в панели и приём комментариев с карточки.
проверка "снимок доски"   python3 scripts/test_build_board.py
проверка "очередь по статусам" python3 scripts/test_work_queue.py
проверка "очередь по важности" python3 scripts/test_queue_importance.py
проверка "доска панели"   python3 harness/panel/test_task_board.py
проверка "приём комментариев" python3 scripts/test_comments_intake.py
проверка "шаги пайплайна" python3 scripts/test_write_step.py
проверка "судья безопасности" python3 scripts/test_security_gate.py
проверка "печать скила" python3 scripts/hooks/test_artifact_seal.py
проверка "носители шагов" python3 scripts/test_step_carriers.py
проверка "докер-уборка" python3 scripts/test_docker_sweep.py
проверка "правка памяти" python3 scripts/test_write_memory.py
# Описание устройства стареет быстрее всего: механизмы заводятся каждую смену.
# Сверка двусторонняя — механизм без описания и описание без механизма красные.
проверка "обновление"     python3 scripts/test_update.py
проверка "доустановка"    python3 scripts/test_install_new.py
проверка "исход одним судьёй" python3 scripts/test_map_from_message_outcome.py
проверка "«не работает» — работа" python3 scripts/test_map_from_message_not_work.py
# Тревоги сторожей: писатель карточек, его форма в приёмке, гейт писателей карты,
# донос в sentinel и общий писатель lib/cards.py.
проверка "писатель тревог"  python3 scripts/test_alert_task.py
проверка "форма --is-green" python3 scripts/test_acceptance_is_green.py
проверка "мутации до принятия" python3 scripts/test_mutations_before_accept.py
проверка "писатели карты в библиотеке" python3 scripts/test_map_writers_library.py
проверка "снятие из карты"  python3 scripts/test_archive_map.py
проверка "дубли в карте"  python3 scripts/test_weed_duplicates.py
проверка "донос тревог sentinel" python3 scripts/test_sentinel_drain.py
проверка "сторож меток: стенд" python3 scripts/test_heartbeat_watch_alerts.py
проверка "писатель карты lib" python3 scripts/test_cards_lib.py
# Ежемесячная ревизия: данные (области и пороги) и порции с курсором.
# Область с маской, под которую не попадает ни один файл, читала бы пустоту и
# отчитывалась «находок нет»; без курсора каждый месяц читались бы те же 15 %.
проверка "конфиг ревизии"  python3 scripts/test_revision_config.py
проверка "порции ревизии"  python3 scripts/test_revision_batches.py
проверка "сверка находок" python3 scripts/test_revision_findings.py
# Самотест САМОГО демона ревизии. Три пробы выше судят части (конфиг, порции,
# сверку находок), а прогон целиком не судил никто — и он краснел восемь суток
# семью путями из десяти, потому что жил за флагом «--всё», который никто не
# запускает по расписанию.
проверка "ревизия-самотест" bash harness/demons/project-revision.sh --selftest
проверка "устройство"     python3 scripts/check-registry.py
# Файл кода вне всех областей ревизии не читает ни один круг — молча. Замер
# 28.09.2026: таких было 6 из 199, среди них сборщик скилов.
проверка "области ревизии" python3 scripts/check-revision-coverage.py
проверка "области-самотест" python3 scripts/check-revision-coverage.py --selftest
проверка "устройство-самотест" python3 scripts/test_registry.py
# Сверка сроков присмотра: реестр против живого конфига сторожа. Без строки
# здесь механизм против молчаливого расхождения сам молчал бы — его не звал
# никто (ревью кода 13.09.2026, К-2).
# Зов модели без --strict-mcp-config отбирает канал у владельца. Сторож команд
# судит вызовы оболочки агента, а демона запускает cron — мимо него; 13.09.2026
# три демона ходили так ежедневно.
проверка "зов модели"     python3 scripts/check-model-call.py
проверка "сроки присмотра" python3 scripts/build-watch-periods.py --проверить
проверка "сроки-самотест"  python3 scripts/test_build_watch_periods.py
# Вход по подписке имеет срок, и владелец о нём не знал (вопрос 11.09.2026).
# Сторож предупреждает ЗАРАНЕЕ: решение, требующее «чтобы кто-то помнил», —
# не решение. Значения токенов в вывод не попадают (проверено самотестом).
проверка "срок входа"      python3 scripts/check-login-expiry.py
# Харнес отдают другим людям: имя чужого продукта в его коде — и утечка, и
# поломка у того, чей продукт зовётся иначе (замер 12.09.2026 — 124 вхождения
# в 20 файлах; гейт импортов и гейт гигиены искали по вшитому имени).
проверка "харнес чист"     python3 scripts/check-harness-clean.py
# Таблица моделей обязана покрывать КАЖДЫЙ шаг пайплайна: шаг без своей модели
# молча уезжает в самое дешёвое. Проверка идёт на БОЕВЫХ файлах — самотест
# гонял её на синтетических, и дыру в живой таблице никто не увидел бы
# (находка ревью кода 12.09.2026).
проверка "модели под шаги"  python3 scripts/model-steps.py
проверка "пайплайн"        python3 scripts/pipeline.py --selftest
проверка "безопасность-самотест" python3 scripts/check-security.py --selftest
# Правила переписки (владелец 11.09.2026) держит сторож внутри tg_send.sh;
# здесь — его самотест, иначе сторож канала стоит без проверки.
проверка "переписка"       python3 scripts/check-channel-messages.py --selftest
проверка "карта"          bash scripts/pre-commit-gate.sh --selftest-devmap
проверка "потеря задач"   python3 scripts/check-map-keeps-tasks.py --selftest
# Замки публикации: их самотест не звал никто, и он молча падал — проба
# задавала настройки окружением, а скрипт перечитывал их из harness.conf.
# Поймано первой живой попыткой публикации 11.09.2026.
проверка "сборка-публичного" python3 scripts/build-public.py --selftest
проверка "сборка-в-секунду" python3 scripts/test_build_public_same_second.py
проверка "манифест-в-сборке" python3 scripts/test_build_public_self_manifest.py
проверка "чистота-самотест" python3 scripts/check-public-clean.py --selftest
проверка "quotepath"      python3 scripts/check-quotepath-flag.py
# Расширения ставит код, а не человек слэш-командой: список жил текстом скила
# со слэш-формами и простоял месяц с нулём установок (замер 12.09.2026).
проверка "расширения"     python3 scripts/check-extensions.py
# CLAUDE.md пакета собирается из данных: правка руками уедет при следующей
# сборке, а до неё файл врёт о том, чем держится каждый инвариант.
проверка "claude.md"      python3 scripts/build-claude-md.py --проверить
# Карточки помощников собираются из ролей и политики: правка руками или роль без
# пересборки — красный, иначе помощник стартует с устаревшей личностью (хук его отвергнет).
проверка "карточки"       python3 scripts/self-prompt.py --карточки --проверить
# Скил — копия куска главы, а генератор зовут КОМАНДОЙ: поправил главу, забыл
# перегенерировать — агент работает по устаревшему правилу и уверен, что прав.
# Вопрос владельца 12.09.2026 про генерацию скилов вскрыл, что сверки не было.
проверка "скилы"          python3 harness/BUILD-SKILLS.py --сверить
проверка "скилы-самотест" python3 scripts/test_skills_build.py
# Живая проверка скила ждёт файл из skill-artifacts.yaml; скил, чья глава этот
# файл не называет, не оставит его никогда.
проверка "файл скила"     python3 scripts/test_skill_names_its_artifact.py
# Уровень вызова помощников: владелец выбирает его ключом AGENT_LEVEL, а
# опечатка в нём не должна выглядеть как обычный уровень (12.09.2026).
проверка "уровень агентов" python3 scripts/test_agent_level.py
# Помощник случаев самотеста: сломанный в «всегда ок», он зеленит всех своих.
проверка "случай самотеста" bash scripts/test_selftest_cases.sh
# Доска состояния: запись с истёкшим сроком не должна выдаваться за правду.
проверка "доска"          python3 scripts/test_board.py
проверка "лимит-журнал"   python3 scripts/limit-in-log.py --selftest
проверка "лимит-сторож"   bash scripts/test_limit_watch.sh
проверка "тишина-лимита"  bash scripts/test_limit_silence.sh
проверка "хеш-деревья"    bash scripts/test_tg_send_commit_trees.sh
проверка "статус-формат"  python3 scripts/test_status_report.py
проверка "дверь-сторожа"  bash scripts/test_warden_channel_door.sh
# Смена сессии без написанной передачи невозможна (владелец 18.10.2026).
проверка "дверь-ротации"  bash scripts/test_warden_handover_gate.sh
# Важный шаг делается моделью из таблицы, а не какой случилась (владелец 18.10).
проверка "модель-шага"    python3 scripts/hooks/test_model_guard.py
# Потолок помощников на задачу по уровню глубины (pipeline.yaml → помощники).
проверка "потолок-помощников" python3 scripts/hooks/test_agent_budget_guard.py
проверка "конец-помощника"   python3 scripts/hooks/test_agent_finish_log.py
# Заявка помощника на файл и предупреждение о чужой (FILE_CLAIM_* в harness.conf).
проверка "заявка-на-файл" python3 scripts/hooks/test_file_claim_guard.py
проверка "роли помощников" python3 scripts/hooks/test_lead_role_guard.py
# План запуска помощников: код решает роль и число исполнителей (правила вложенности, 30.10.2026).
проверка "план запуска"   python3 scripts/test_launch_plan.py
проверка "сторож плана"   python3 scripts/hooks/test_launch_plan_guard.py
проверка "поле главы"     python3 scripts/test_nesting_chapter_field.py
проверка "метка части"    python3 scripts/hooks/test_agent_budget_part_label.py
проверка "носитель-хуков" python3 scripts/test_live_check_hook.py
проверка "носитель-входящих" python3 scripts/test_live_check_inbox.py
проверка "отправители"    python3 scripts/check-owner-senders.py
проверка "отправители-сам" python3 scripts/check-owner-senders.py --selftest
# Остатки в пакете и ключи образца конфига: оба гейта судят дерево, а не пробу.
проверка "остатки пакета" python3 scripts/check-package-leftovers.py
проверка "ключи образца"  python3 scripts/check-sample-config-keys.py
проверка "память-в-гит"   bash scripts/test_memory_commit.sh
# Каждое сообщение владельца доезжает до панели сразу, и все писатели в панель
# идут одной дверью под общим локом: иначе тексты склеиваются до Enter.
проверка "лок панели"      bash scripts/test_pane_lock_instant.sh
проверка "метки доставки"  bash scripts/test_delivery_marks.sh
проверка "голос отдельным обработчиком"  bash scripts/test_voice_worker.sh
# Каждое принятое сообщение имеет конечную метку; «принял» не держит цикл приёма.
проверка "след доставки (проба)"  bash scripts/test_delivery_trace.sh
проверка "след доставки"  python3 scripts/delivery-latency.py --след --ворота
# Приём канала не должен вставать от зависшего tmux и сторож обязан это видеть.
проверка "приём-зависание" bash scripts/test_dispatcher_hang.sh
проверка "сторож приёма"   bash scripts/test_dispatcher_stall_watch.sh
# Ручка «слать сразу» (PANE_SEND_NOW): аккорд только после успешного толчка и
# только при ключе 1, прочитанном в момент толчка; в панели она видна и пишется.
проверка "толчок-сразу"    bash scripts/test_pane_send_now.sh
проверка "дверь толчка: пауза, ротация, лимит"    bash scripts/test_pane_door_pause.sh
проверка "ручка-сразу"     python3 harness/panel/test_send_now_switch.py
# У факта один судья: кто о факте говорит — тот его и спрашивает. Гейт написан
# под три ночных отказа 12.09.2026, у которых был один корень.
проверка "признаки"       python3 scripts/check-signals.py
проверка "признаки-сам"   python3 scripts/test_signals.py
# Настройки панели: пути читаются при обращении, а не при запуске службы.
проверка "настройки панели" python3 harness/panel/test_settings.py
# Кнопки в меню бота: меню живёт на стороне Telegram, его может затереть.
проверка "команды бота"   python3 scripts/test_bot_commands.py
проверка "расширения-сам" python3 scripts/check-extensions.py --selftest
проверка "публикация"     bash scripts/publish.sh --selftest
проверка "витрина-статусы" python3 scripts/vitrina_status.py --selftest
# Две проверки, написанные 12.09.2026, не звал НИКТО — ни ворота, ни pre-commit
# (ревью кода, №8). Проверка, которую не запускают, не сторожит.
проверка "данные сборки"  python3 scripts/test_public_build.py
# Живая проверка механизмов (его задание 15.10): обёртка следа и разрез по
# классам. Без вызова из ворот пробы не сторожат ничего.
проверка "обёртка живой проверки" bash scripts/test_live_check_sh.sh
проверка "классы живой проверки"  python3 scripts/test_live_check_classes.py
проверка "след живой проверки"   python3 scripts/test_live_check_trace.py
проверка "тексты мутаций"        bash scripts/test_probe_mutations.sh
проверка "больная половина"      python3 scripts/test_live_check_sick.py
проверка "живая проверка демонов" python3 scripts/test_live_check_daemon.py
проверка "живая проверка скилов" python3 scripts/test_live_check_skill.py
проверка "живой вызов C2"        python3 scripts/test_live_check_tool.py
# Сверка текстов — в ОБЫЧНОМ прогоне, а не за флагом --всё: мутация с
# отставшим текстом не краснеет и не жалуется, и круг правок оставляет её
# позади незамеченной (13.10.2026 — десять из 78).
проверка "мутации стоят"         bash scripts/mutation-probe.sh --сверить-тексты
# Новая проба без записи мутаций: оба прибора выше судят только строки, которые
# в данных ЕСТЬ, и молчали о четырёх файлах проб (замер 27.10.2026).
проверка "пробы без мутаций"     python3 scripts/check-probe-mutations.py
проверка "проба ворот мутаций"   python3 scripts/test_check_probe_mutations.py
проверка "события: проба записи" python3 scripts/test_events_lib.py
проверка "события: проба разбора" python3 scripts/test_events_cli.py
проверка "события: проба ворот" python3 scripts/test_events_gates.py
# Догоняние публичной ветки: пока тот же общий гит правят две машины владельца,
# расхождение повторяется, и переносить его правки руками — терять их молча.
проверка "догоняние публичной" python3 scripts/catch-up-public.py --selftest
проверка "юнит панели"    bash scripts/panel-unit.sh --selftest
# Рубеж панели: код опирается на конфиг nginx в четырёх местах, а самого
# конфига не было ни в репозитории, ни в пакете. Живая улика 13.09.2026 —
# панель отвечала на ЛЮБОЙ Host, потому что default_server не объявлял никто.
# Скрипты ПАКЕТА судятся отдельно: они лежат в другом дереве со своим git, и
# pre-commit проекта их не видит. 13.09.2026 кириллическое имя переменной в
# USTANOVIT.sh уронило живой прогон установки — гейт молчал, потому что смотрел
# не туда.
проверка "кириллица-пакет" python3 scripts/check-cyrillic-in-names.py --пакет
проверка "вызов модели" python3 scripts/check-daemon-calls-model.py
проверка "вызов-самотест" python3 scripts/check-daemon-calls-model.py --selftest
проверка "обёртка демона" bash scripts/test_claude_demon.sh
проверка "обёртка демона: поток" bash scripts/test_demon_stream_json.sh
проверка "установка панели" python3 scripts/test_panel_install.py
проверка "установка-самотест" python3 scripts/test_panel_install.py --selftest
проверка "рубеж панели"   python3 scripts/check-panel-boundary.py
проверка "рубеж-самотест" python3 scripts/check-panel-boundary.py --selftest
проверка "сборщик рубежа" bash scripts/nginx-panel.sh --selftest
# Панель работает под своим пользователем: файл, который она показывает, но не
# может прочитать, даёт владельцу «нет данных» при живом и свежем файле.
проверка "права панели"   python3 scripts/check-panel-permissions.py
# Команда, напечатанная человеку, обязана вести к существующему файлу: после
# переименования каталогов семь документов звали scripts/возможность.sh —
# поймано живым прогоном установки в чистом контейнере.
проверка "команды в док."  python3 scripts/check-commands-in-docs.py
проверка "команды-самотест" python3 scripts/check-commands-in-docs.py --selftest
проверка "права-самотест" python3 scripts/check-panel-permissions.py --selftest
# Признак согласия владельца — сторож И-2 и полуавтомата: «делай деплой, да»
# не должно разрешать отправку, «выкат после 3 этапа» — не согласие вовсе.
# Проверка была написана 03.09 и до ревизии 10.09 её не запускал НИКТО: ни
# ворота, ни pre-commit, ни демон. Проверка, которую не запускают, не сторожит.
проверка "согласие"       python3 scripts/test_deploy_guard_consent.py
# У пишущего пути обязан быть хозяин и потолок: «мусор» — один дефект, а не
# семь разных уборок (замечание владельца П-3).
проверка "удержание"      python3 scripts/check-retention.py
# Предложения ревизии памяти обязаны разбираться, а не копиться: демон без
# принуждения — половина механизма (замечание владельца П-5).
проверка "предложения"    python3 scripts/check-proposals.py
# Режим хакатон: пробы задачи идут в полных воротах; в быстрое подмножество
# (harness/config/gates-hackathon.yaml) они не входят.
проверка "хакатон" python3 scripts/test_hackathon.py
проверка "хакатон: карта" bash scripts/test_hackathon_map_gate.sh
проверка "хакатон: ворота" bash scripts/test_hackathon_gates.sh
проверка "хакатон: метка" bash scripts/test_hackathon_mark.sh
проверка "хакатон: ручка" python3 scripts/test_hackathon_switch.py
проверка "хакатон: меню" bash scripts/test_hackathon_menu.sh
проверка "маска секретов" bash scripts/test_check_secrets_mask.sh
проверка "секреты под маской" python3 scripts/test_security_secrets_masked.py
проверка "скорость: судья" python3 scripts/test_speed_mode.py
проверка "скорость: сторож" bash scripts/test_warden_speed.sh

# Самотесты живут врозь от гейтов: они долгие, и на каждом коммите не нужны.
# Список явный — искать «кто понимает --selftest» грепом значило бы решать
# грепом, что именно проверяется; такое решение не читается и молча худеет.
#
# Проба восстановления — с 28.09.2026: её 20 проб были записаны в карточке
# задачи и в паспорте носителя, а гонять их не был обязан никто, то есть
# доказательство И-1 снова держалось на внимании (находка F-09 ревью Оси В).
#
# Приёмка и строитель проверок стоят здесь с 27.09.2026: их 26 проб не гонял
# НИКТО, а реестр при этом утверждал «проверяется: … путей» — утверждение без
# исполнителя ([[a-mechanism-that-needs-a-human-is-dead]], находка G-10 ревью).
if [ "${1:-}" = "--всё" ]; then
    REZHIM="всё"
    echo "--- самотесты ---"
    for f in harness/demons/sentinel.sh harness/demons/session-warden.sh \
                harness/demons/backup.sh \
                harness/demons/acceptance-watch.sh \
                harness/demons/check-builder.sh \
                harness/demons/heartbeat-watch.sh harness/demons/task-closer.sh \
                harness/demons/memory-revision.sh \
                harness/demons/stack-watch.sh \
                harness/demons/update-watch.sh \
                harness/demons/nightly-mutations.sh \
                harness/demons/tokens-collector.sh scripts/tg-dispatcher.sh \
                scripts/start-agent.sh scripts/capability.sh \
                scripts/handover-check.sh scripts/check-secrets.sh \
                scripts/restore-probe.sh \
                scripts/model-for.sh; do
        # Демоны берут код из PROJECT_DIR (окружение старше паспорта): без
        # него их самотесты в прогоне по снимку гоняли бы живое дерево.
        PROJECT_DIR="$ROOT" проверка "$(basename "$f" .sh)" bash "$f" --selftest
    done
    # Свежесть стека: замер и гейт реестра (Ф1); пробы сводки и демона идут через stack-watch.sh выше.
    проверка "свежесть стека" python3 scripts/stack_freshness.py --selftest
    # Обратный ход ревизии: у каждого правила — механизм, прибор и свежий след.
    # Строка без механизма важнее любой находки осмотра (замечание М-1).
    проверка "покрытие"       python3 scripts/invariant-coverage.py
    # Ф4 ревизии, класс 2: исход проверки решает механизм, а не окружение
    # хозяина. Гоняет каждую лёгкую проверку трижды — 9 минут на 126 проверок,
    # поэтому только здесь, за флагом --всё. Законное — данные
    # harness/config/host-independence.yaml.
    # Только у нашей установки (HOST_INDEPENDENCE_CHECK=1): копиям проверка
    # незачем. Блоком `if`, а не `&&` в строку: запись через && выпадает из
    # разбора СТРОКА_ПРОВЕРКИ, и данные прибора разошлись бы с воротами.
    # Второй круг гоняет только изменённое с метки — см. check-host-independence.py.
    if [ "${HOST_INDEPENDENCE_CHECK:-0}" = "1" ]; then
        проверка "хозяин машины"  python3 scripts/check-host-independence.py
    fi
    проверка "хозяин-самотест" python3 scripts/check-host-independence.py --selftest
    # Какой круг берёт прибор и что выключает ключ судит свой стенд, поэтому и у копий.
    проверка "хозяин-круг" python3 scripts/test_host_independence_scope.py
    # Ф5 ревизии: установка с нуля доказывается прогоном, а не памятью. Около
    # двух минут: поднимает свой контейнер, ставит харнес, судит по следам.
    проверка "установка с нуля" bash scripts/install-probe.sh
    проверка "установка-проба-самотест" bash scripts/install-probe.sh --selftest
    # Живая проверка дозора позднего выхода: поднимает свою tmux-сессию,
    # убивает её и смотрит, поднял ли дозор преемника. Лежала среди демонов и
    # не запускалась никем (ревизия 10.09.2026).
    проверка "дозор выхода"   bash scripts/test_late_exit_watch.sh
    # Обвязка сторожа сессий целым прогоном на стенде: что из решений «работы
    # нет, смена у приглашения» доходит до канала и до панели. Поднимает свою
    # tmux-сессию, поэтому за флагом --всё, как дозор выхода.
    проверка "сторож у приглашения" bash scripts/test_warden_blocked_idle.sh
    # «Если пауза то пауза» (владелец 20.10): на паузе решение о ротации не
    # доходит до двери, явный --rotate-now идёт. Своя tmux-сессия — за --всё.
    проверка "пауза гасит ротацию" bash scripts/test_warden_pause_rotation.sh
    # Работа помощников — это работа (25.10.2026): судья, сторож целиком, дверь
    # канала и limit-watch на стенде. Своя tmux-сессия — за --всё.
    проверка "работа помощников" bash scripts/test_warden_subagent_work.sh
    # Лимит подписки держится — сторож в панель молчит (30.10.2026): единый судья
    # и сторож целиком на записанном хвосте транскрипта. Своя tmux-сессия — за --всё.
    проверка "тишина в лимите" bash scripts/test_warden_limit_silence.sh
    # Пульс сторожа слышен владельцу (03.11.2026): судья лимита не держит ложную метку,
    # сторож сам шлёт статус, «молчу» пишется в лог с причиной. Своя tmux-сессия — за --всё.
    проверка "пульс сторожа" bash scripts/test_warden_pulse.sh
    # Сторож без транскриптов и журнал панели агента: свои tmux-сессии на стенде — за --всё.
    проверка "сторож без транскриптов" bash scripts/test_warden_no_transcripts.sh
    проверка "журнал панели агента" bash scripts/test_agent_pane_log.sh
    # Уборка стендов проб диспетчера (04.11.2026): прерванная проба не оставляет
    # диспетчеров-сирот. Гоняет пять проб с настоящим диспетчером — минуты, за --всё.
    проверка "уборка стендов проб" bash scripts/test_probe_stand_sweep.sh
    # Третье состояние гейта (жёлтый, код 77) — механизм обкатки: без него
    # новый гейт обязан молчать, а краснящий зря — отключают.
    проверка "жёлтый в pre-commit" bash scripts/pre-commit-gate.sh --selftest-yellow
    проверка "жёлтый в воротах"    bash scripts/gates.sh --selftest
    # Секунды каждой проверки пишутся в gates-timing.jsonl: долгую видно
    # числом, а не догадкой. Проба гоняет ворота на стенде — за --всё.
    проверка "секунды проверок" python3 scripts/test_gates_timing.py
    # Прогон по снимку: правка живого дерева не меняет итог, снимок убирается.
    проверка "прогон по снимку" bash scripts/test_gates_snapshot.sh
    # Пять бумажных гейтов хука до коммита одним проходом: проба строит свой
    # репозиторий с текстом настоящего хука — за --всё.
    проверка "предварительная проверка коммита" python3 scripts/test_precheck.py
    # Слово владельца «да» тратится ПРОХОДОМ ворот, а выкат идёт дальше:
    # упади он на секретах или смоуке — согласие сгорело на действии, которого
    # не было. Проба поднимает свой стенд и судит состояние согласия.
    проверка "возврат согласия" bash scripts/test_deploy_consent_return.sh
    # Объявленный продукт выкатывается только через deploy.sh: отказ при пустой
    # секции STACK_*, тег и секреты по репозиторию продукта, откат при красном health.
    проверка "выкат продукта"   bash scripts/test_deploy_product.sh
    # Пробы правок с соседней установки (18.10.2026): каждая судит свой механизм
    # на стенде, больной случай первым.
    проверка "дверь-демонов"    bash scripts/test_demon_channel_door.sh
    # Пять сторожей волны 1 заводят задачу модели и гасят её на зелёном; идёт ~4 минуты.
    проверка "сторожа-задачи"   bash scripts/test_watchers_wave2.sh
    # Семь сторожей волны 3 (приёмка, строитель, расширения, ревизия памяти,
    # здоровье, страж, сторож сессий) — то же; сторож сессий на своём tmux, ~6 минут.
    проверка "сторожа-задачи-3" bash scripts/test_watchers_wave3.sh
    # Предел ночного прогона: настоящий timeout на стенде, ~1 минута.
    проверка "ночной-предел"    bash scripts/test_nightly_gates_ceiling.sh
    # Правка ночного демона на ходу не роняет прогон; выход без итога назван, ~15 с.
    проверка "ночной-правка"    bash scripts/test_nightly_gates_live_edit.sh
    # Мутации идут отдельным недельным запуском со своим пределом; проба на стенде, ~1 минута.
    проверка "ночные-мутации"  bash scripts/test_nightly_mutations.sh
    # Замок и очередь недельного прогона: флаг .want, ожидание, весть «не стартовал»; стенд, доли минуты.
    проверка "замок-мутаций"  bash scripts/test_nightly_mutations_lock.sh
    # Пределы самой пробы: таймаут запуска, мягкая остановка, выпавшие записи; настоящий прибор на стенде, ~10 с.
    проверка "пределы-мутаций"  bash scripts/test_mutation_probe_limits.sh
    проверка "тревога-диска"    python3 scripts/test_disk_report.py
    проверка "уборка /tmp"      python3 scripts/test_tmp_sweep.py
    проверка "коммит карты"     bash scripts/test_devmap_selfheal.sh
    проверка "stop-хук"         bash scripts/hooks/test_stop_reminder.sh
    проверка "ответ в канал"    bash scripts/hooks/test_channel_reply_guard.sh
    проверка "вид работы"       bash scripts/test_model_for.sh
    проверка "имена-задачи"     python3 scripts/test_check_file_names.py
    проверка "спутник отказов"  python3 scripts/test_inbox_digest.py
    проверка "остатки-проба"    python3 scripts/test_package_leftovers.py
    проверка "ключи-проба"      python3 scripts/test_sample_config_keys.py
    проверка "голосовые"        bash scripts/test_install_voice.sh
    проверка "вердикт-согласия" python3 scripts/test_patterns_verdict.py
    проверка "одобрение-спеки"  python3 scripts/test_spec_approval_forms.py
    проверка "набор-памяти"     python3 scripts/test_memory_to_package.py
    проверка "отправители-ход"  python3 scripts/test_owner_senders.py
    проверка "сводка ресурсов"  python3 scripts/test_resource_report.py
    # Пробы, которых не звал никто: без строки здесь «кто зовёт» и «население кода»
    # краснят, а проба выглядит живой. Окно перезагрузки идёт ~8 минут, прочие — до 2,5.
    проверка "порог ревизии памяти" bash scripts/test_memory_revision_threshold.sh
    проверка "страж: решение"   python3 scripts/test_sentinel_resolve.py
    проверка "квитанции принял" bash scripts/test_ack_receipts.sh
    проверка "очередь ротации"  python3 scripts/hooks/test_rotation_queue_guard.py
    проверка "рестарт при помощниках" python3 scripts/hooks/test_guard_bash_restart_helpers.py
    проверка "окно перезагрузки" bash scripts/test_server_restart_window.sh
    проверка "ротация ждёт помощников" bash scripts/test_warden_rotation_waits_helpers.sh
    проверка "сервер поднялся"  python3 scripts/test_boot_check.py
    проверка "страж после перезагрузки" python3 scripts/test_sentinel_after_restart.py
    проверка "журналы реестра"  bash scripts/test_check_registry_logs.sh
    проверка "уборка диска-самотест" bash scripts/test_disk_cleanup_selftest.sh
    проверка "пауза копит"      bash scripts/test_dispatcher_pause_accumulate.sh
    проверка "замок живой проверки" python3 scripts/test_live_check_flock.py
    проверка "карта из сообщения" python3 scripts/test_map_from_message_chapter.py
    проверка "ночь сломана: гашение" bash scripts/test_nightly_gates_broken_resolve.sh
    проверка "пинок окна"       bash scripts/test_pane_kick_confirm.sh
    проверка "лимит одним числом" bash scripts/test_warden_limit_one_number.sh
    # Паспорта этих проб обещают ворота: без строки здесь обещание пусто. Секунды
    # соло — от 1 до 180 (кнопка согласия), поэтому только за флагом --всё.
    проверка "библиотека-гейт"  python3 scripts/test_process_library_gate.py
    проверка "библиотека процессов-проба" python3 scripts/test_process_library.py
    проверка "сборка расписания" python3 scripts/test_build_system_jobs.py
    проверка "служба проверок"  python3 scripts/test_checks_service.py
    проверка "согласие кнопкой" python3 scripts/test_consent_button_only.py
    проверка "двери согласия"   python3 scripts/test_consent_doors.py
    проверка "признаки просьбы" python3 scripts/test_consent_ask_signs.py
    проверка "полосы очереди"   python3 scripts/test_work_queue_lanes.py
    проверка "страница процессов" python3 harness/panel/test_processes_page.py
    проверка "сводка сторожа меток" python3 scripts/test_heartbeat_held_line.py
    проверка "след вне репозитория" python3 scripts/test_live_check_trace_outside_repo.py
    проверка "карта: три исхода" python3 scripts/test_map_from_message_three_outcomes.py
    проверка "шаг мутаций"      python3 scripts/test_mutations_step.py
    проверка "показ диспетчера" python3 scripts/test_process_dispatcher_v2.py
    проверка "ревью спеки"      python3 scripts/test_spec_review.py
    проверка "суть спеки"       python3 scripts/test_spec_human_block.py
    проверка "фоновая обёртка"  bash scripts/test_run_background.sh
    проверка "порт после смены" bash scripts/test_shift_leftover_port.sh
    проверка "пинок сторожа"    bash scripts/test_warden_kick.sh
    проверка "начало сторожа"   bash scripts/test_warden_pane_flag.sh
    for f in scripts/work-queue.py scripts/compare-with-package.py \
                scripts/download_attachment.py scripts/orders.py \
                scripts/check-cyrillic-in-names.py \
                scripts/check-failure-under-set-e.py \
                scripts/check-login-expiry.py \
                scripts/check-channel-messages.py \
                scripts/pipeline.py \
                scripts/check-file-names.py \
                scripts/check-pipeline.py \
                scripts/check-tests-first.py \
                scripts/attention-word.py scripts/check-harness-clean.py \
                scripts/acceptance.py; do
        проверка "$(basename "$f" .py)" python3 "$f" --selftest
    done
fi

YELLOW_TAIL=""
[ "$YELLOW" -gt 0 ] && YELLOW_TAIL=", жёлтых $YELLOW (обкатка)"
echo "=== итог: зелёных $GREEN, красных $RED$YELLOW_TAIL ==="
# Итог — строкой в журнал: витрина панели показывает владельцу, когда ворота
# гонялись в последний раз и чем кончились. Без этой строки она честно говорит
# «нет данных», но владелец так и не видит состояния проверок (находка I6
# ревью спеки панели). Отказ записи не роняет ворота: журнал — наблюдение.
# Режим — в записи, иначе полный прогон и обычный в журнале НЕОТЛИЧИМЫ:
# 28.09.2026 приёмка не смогла судить карточку, требующую «--всё», по следу
# ворот именно поэтому ([[freshness-by-mtime-is-false]] того же класса —
# след есть, а сказать по нему нечего).
# Выборка — свой режим: её «красных 0» не годится за итог полного прогона.
[ -n "$ONLY_CHECKS" ] && REZHIM="выборка"
printf '{"ts":"%s","режим":"%s","зелёных":%s,"красных":%s}\n' \
    "$(date -Is)" "$REZHIM" "$GREEN" "$RED" \
    >> "$ITOG_LOG" 2>/dev/null || true
exit "$RED"
