#!/usr/bin/env bash
#
# pre-commit-hook.sh — единственный источник истины по гейтам коммита.
#
# Откуда взят: UNIFIED/templates/pre-commit-hook.sh (живой хук боевого сервера).
# Что изменено: тесты backend/frontend и тиры T1–T3 убраны — продукта на чистом
# сервере нет, гейтить нечего; вместо них быстрый набор под харнес: секреты по
# staged, гигиена контекста, синтаксис .sh/.py, схема dev-map.yaml; добавлен
# журнал прогона в $LOG_DIR/gates.jsonl (его читает evo-collector). Линтеры
# кода продукта подключатся данными (PRODUCT_LINT_CMD в harness.conf) — пока
# ключ пуст, хук говорит об этом вслух, а не молчит. Тесты стека вернутся из
# шаблона с первой задачей продукта, улики там.
#
# Правки ревью 09.08.2026:
#   К-4 — все проверки staged-файлов гоняются по содержимому ИНДЕКСА
#   (git show :файл во временную копию), не рабочего дерева: коммитится
#   индекс; чистый диск при битом staged раньше давал ложный зелёный.
#   Б-3 — схема dev-map несёт статусный гейт И-4: задача done обязана нести
#   tested_at (дата подтверждения владельцем) либо owner_testable: false
#   (внутреннее — сразу done, норма UNIFIED/07). Больной случай встроен:
#   pre-commit-hook.sh --selftest-devmap.
#
# Улика шаблона: рядом с хуком жили gates.yaml и policy.yaml, описывавшие то же
# самое ещё раз. Замер 08.08.2026: их не читал никто; объявленные там бюджеты
# не мерились ничем. Второй источник истины, который никто не читает, — это не
# документация, а ложь про устройство. Гейты живут здесь и только здесь.
#
# Установка: ln -sf "$PROJECT_DIR/scripts/pre-commit-hook.sh" .git/hooks/pre-commit
# Ненулевой код возврата отменяет коммит.
set -euo pipefail

INSTALL_CONF="${HARNESS_INSTALL_CONF:-/etc/harness/install.conf}"
HARNESS_CONF="${HARNESS_CONF:-/etc/harness/harness.conf}"
# Конфиги грузятся так, чтобы ОКРУЖЕНИЕ было старше файла: иначе проба с
# подставным путём судит БОЕВУЮ установку (улика 12.09.2026 — команда смены
# модели ушла в рабочую панель агента).
# shellcheck disable=SC1091
# Путь берётся по РЕАЛЬНОМУ файлу (readlink -f): pre-commit подключён в
# .git/hooks симлинком, и dirname дал бы .git/hooks, где библиотеки нет.
# Поймано первым же коммитом после правки, 12.09.2026.
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/konf.sh"
konf_zagruzit PROJECT_DIR LOG_DIR TMUX_SESSION SECRETS_DIR AGENT_START_CMD
LOG_DIR="${LOG_DIR:-/var/log/harness}"
GATES_LOG="$LOG_DIR/${GATES_LOG_NAME:-gates.jsonl}"

# ── Валидатор схемы dev-map.yaml ────────────────────────────────────────────
# Один текст на боевой гейт и самотест: два экземпляра валидатора разъехались
# бы на первой правке. Кривой манифест — кривой дашборд у владельца.
DEVMAP_VALIDATOR=$(cat <<'PYEOF'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("dev-map.yaml в коммите, а pyyaml не установлен — гейт не может проверить схему.")
    print("Поставь: sudo apt-get install -y python3-yaml")
    sys.exit(1)

# Дубль ключа yaml.safe_load берёт молча последним: так шесть пунктов
# чек-листа pr.движок исчезли из карты, оставшись в файле (20.08.2026).
дубли = []


def _mapping(loader, node, deep=False):
    видели = set()
    for ключ, _ in node.value:
        имя = loader.construct_object(ключ, deep=deep)
        if имя in видели:
            дубли.append(f"строка {ключ.start_mark.line + 1}: ключ «{имя}» задан дважды — YAML оставит только последний")
        видели.add(имя)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


class Лоадер(yaml.SafeLoader):
    construct_mapping = _mapping


data = yaml.load(open(sys.argv[1], encoding="utf-8"), Лоадер)
errors = list(дубли)
# «принято» — доказано МАШИНОЙ: команда проверки из карточки отработала и дала
# ожидаемое. Владелец 11.09.2026: «Я не хочу лично все проверять, в этом и
# смысл, надо только то, что не может проверить система». Это не подмена его
# «done»: done по-прежнему ставится по его слову (И-4), а «принято» говорит
# ровно то, что есть, — доказательство машинное, и вот команда.
STATUSES = {"done", "принято", "test", "wip", "plan"}
# Глубина задачи: уровень — цена ошибки, характер — чем работа является.
# Списки берутся из пульта владельца (harness/config/пайплайн.yaml), а не
# вшиты сюда: иначе у факта снова окажется два судьи (находка ревью C-7).
УРОВНИ = {"мелкая", "обычная", "крупная"}


def _характеры():
    """Список характеров из пульта владельца.

    Валидатор приходит сюда СТРОКОЙ (python3 -c), и `__file__` в нём нет:
    корень ищется от пути карты, который передан аргументом, а при разборе
    временной копии (pre-commit кладёт её в $STAGE_TMP) — от текущего
    каталога. Не нашли пульт — список пуст, и характер не судится: молчать
    честнее, чем краснеть на каждой карточке из-за своего же промаха.
    """
    кандидаты = []
    if len(sys.argv) > 1:
        кандидаты.extend(Path(sys.argv[1]).resolve().parents)
    кандидаты.append(Path.cwd())
    кандидаты.extend(Path.cwd().parents)
    for корень in кандидаты:
        пульт = корень / "harness" / "config" / "пайплайн.yaml"
        if пульт.exists():
            try:
                import yaml as _y
                дано = _y.safe_load(пульт.read_text(encoding="utf-8"))
                return set((дано or {}).get("характеры") or {})
            except (OSError, ValueError):
                return set()
    # Пульта не нашли — проверка характера отключается. Молчать об этом
    # нельзя: гейт, который перестал проверять, неотличим от гейта, которому
    # нечего сказать (ревью кода 13.09, I-2).
    print("[dev-map] ⚠ пульт harness/config/пайплайн.yaml не найден — "
          "значения поля «характер» не проверяю", file=sys.stderr)
    return set()


ХАРАКТЕРЫ = _характеры()

if not isinstance(data, dict):
    print("манифест не словарь"); sys.exit(1)
for field in ("version", "lines", "layers", "queue"):
    if field not in data:
        errors.append(f"нет обязательного поля верхнего уровня: {field}")

status_by_id = {}
for line in data.get("lines") or []:
    for f in ("id", "layer", "name", "tasks"):
        if f not in line:
            errors.append(f"направление {line.get('id', '?')}: нет поля {f}")
    for t in line.get("tasks") or []:
        tid = t.get("id", "?")
        for f in ("id", "status", "title", "summary"):
            if f not in t:
                errors.append(f"задача {tid}: нет поля {f}")
        st = t.get("status")
        if st not in STATUSES:
            errors.append(f"задача {tid}: статус «{st}» вне {sorted(STATUSES)}")
        status_by_id[tid] = st
        # И-4: done — только после подтверждения владельца. tested_at — дата
        # подтверждения; owner_testable: false — внутренняя работа, владельцу
        # нечего щупать, сразу done — норма (UNIFIED/07).
        if st == "done" and not t.get("tested_at") and t.get("owner_testable") is not False:
            errors.append(f"задача {tid}: done без tested_at: подтверждение владельца не зафиксировано (И-4)")
        # «принято» без названной команды — то же самое «поверь на слово»,
        # от которого статус и заводился. Команда обязана быть в карточке.
        if st == "принято":
            проверка = t.get("проверка") or {}
            if not (isinstance(проверка, dict) and проверка.get("команда")):
                errors.append(f"задача {tid}: принято без поля проверка.команда — "
                              f"машинное доказательство обязано называть команду")
            if not t.get("принято_когда"):
                errors.append(f"задача {tid}: принято без принято_когда")
        # Глубина задачи: поле назначается при взятии в работу и судится здесь.
        # Форма — {уровень, характер, кем, когда}; неизвестное значение красит
        # карту, иначе опечатка живёт молча (проверено 13.09: карточка с
        # «уровень: несуществующий» проходила валидатор с кодом 0).
        глубина = t.get("глубина")
        if глубина is not None:
            if not isinstance(глубина, dict):
                errors.append(f"задача {tid}: глубина должна быть словарём "
                              f"{{уровень, характер, кем, когда}}")
            else:
                уровень_ = глубина.get("уровень")
                характер_ = глубина.get("характер")
                if уровень_ not in УРОВНИ:
                    errors.append(f"задача {tid}: глубина.уровень «{уровень_}» "
                                  f"вне {sorted(УРОВНИ)}")
                if ХАРАКТЕРЫ and характер_ not in ХАРАКТЕРЫ:
                    errors.append(f"задача {tid}: глубина.характер «{характер_}» "
                                  f"вне {sorted(ХАРАКТЕРЫ)} (пульт: harness/config/пайплайн.yaml)")
                if not глубина.get("кем"):
                    errors.append(f"задача {tid}: глубина без «кем» — кто назначил")
                if not глубина.get("когда"):
                    errors.append(f"задача {tid}: глубина без «когда»")
        # done:true в чек-листе обязан иметь done_at и summary (правило из шаблона карты)
        for item in t.get("checklist") or []:
            if item.get("done") and not (item.get("done_at") and item.get("summary")):
                errors.append(f"задача {tid}: пункт чек-листа done без done_at/summary")

# queue — только живое: задача в done/test из очереди удаляется при той же правке
for qid in data.get("queue") or []:
    st = status_by_id.get(qid)
    if st is None:
        errors.append(f"queue ссылается на несуществующую задачу: {qid}")
    elif st in ("done", "test"):
        errors.append(f"queue держит закрытую задачу {qid} (статус {st}) — удали из очереди")

if errors:
    for e in errors:
        print(f"[dev-map] ✗ {e}")
    sys.exit(1)
print("[dev-map] схема в порядке")
PYEOF
)

# Валидатор схемы на ОТДЕЛЬНОМ файле: им пользуются демоны, чьи агенты правят
# карту. Одного yaml.safe_load им мало — дубль ключа он берёт молча последним,
# и 11.09.2026 такой дубль остановил ВСЕ коммиты, включая сохранение памяти с
# панели владельца. Валидатор здесь один на всех: второй экземпляр разъехался
# бы на первой правке.
if [ "${1:-}" = "--devmap-only" ]; then
    [ -n "${2:-}" ] || { echo "--devmap-only: нужен путь к карте" >&2; exit 2; }
    python3 -c "$DEVMAP_VALIDATOR" "$2"
    exit $?
fi

# ── Самотест статусного гейта И-4 (Б-3): больной и здоровый случай ──────────
if [ "${1:-}" = "--selftest-devmap" ]; then
    T=$(mktemp -d)
    trap 'rm -rf "$T"' EXIT
    # Больная фикстура: done без tested_at при owner_testable: true — красный.
    cat > "$T/sick.yaml" <<'EOF'
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.done
        status: done
        owner_testable: true
        title: "Закрыта без подтверждения"
        summary: "обязана дать красный по И-4"
EOF
    if OUT=$(python3 -c "$DEVMAP_VALIDATOR" "$T/sick.yaml" 2>&1); then
        echo "САМОТЕСТ ПРОВАЛЕН: done без tested_at прошёл гейт (И-4 не держится)"
        echo "$OUT"; exit 1
    fi
    echo "$OUT" | grep -q 'done без tested_at' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: красный не про И-4:"; echo "$OUT"; exit 1; }
    # БОЛЬНОЙ СЛУЧАЙ: «принято» без названной команды — то же «поверь на
    # слово», от которого статус и заводился (владелец 11.09.2026).
    cat > "$T/принято-без-команды.yaml" <<'EOF'
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.принято
        status: принято
        принято_когда: "2026-09-11"
        title: "Закрыта машиной, но чем — неизвестно"
        summary: "обязана дать красный"
EOF
    if OUT=$(python3 -c "$DEVMAP_VALIDATOR" "$T/принято-без-команды.yaml" 2>&1); then
        echo "САМОТЕСТ ПРОВАЛЕН: «принято» без команды прошло гейт"
        echo "$OUT"; exit 1
    fi
    echo "$OUT" | grep -q 'принято без поля проверка.команда' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: красный не про машинное доказательство:"; echo "$OUT"; exit 1; }
    # БОЛЬНОЙ СЛУЧАЙ ревью (I4): оба образца несли принято_когда, поэтому
    # проверку его наличия можно было удалить целиком — самотест оставался
    # зелёным. Третий образец: команда есть, даты нет.
    cat > "$T/принято-без-даты.yaml" <<'EOF'
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.принято
        status: принято
        проверка:
          команда: "echo ЗЕЛЁНЫЙ"
          ждём: "ЗЕЛЁНЫЙ"
        title: "Команда есть, даты нет"
        summary: "обязана дать красный"
EOF
    if OUT=$(python3 -c "$DEVMAP_VALIDATOR" "$T/принято-без-даты.yaml" 2>&1); then
        echo "САМОТЕСТ ПРОВАЛЕН: «принято» без даты прошло гейт"
        echo "$OUT"; exit 1
    fi
    echo "$OUT" | grep -q 'принято без принято_когда' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: красный не про дату приёмки:"; echo "$OUT"; exit 1; }

    # Здоровая: «принято» с командой и датой — зелёный.
    cat > "$T/принято-ок.yaml" <<'EOF'
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.принято
        status: принято
        принято_когда: "2026-09-11"
        проверка:
          команда: "echo ЗЕЛЁНЫЙ"
          ждём: "ЗЕЛЁНЫЙ"
        title: "Закрыта машиной, команда названа"
        summary: "обязана пройти гейт"
EOF
    python3 -c "$DEVMAP_VALIDATOR" "$T/принято-ок.yaml" >/dev/null \
        || { echo "САМОТЕСТ ПРОВАЛЕН: «принято» с командой не прошло"; exit 1; }

    # Здоровая фикстура: тот же done, но с датой подтверждения — зелёный.
    cat > "$T/ok.yaml" <<'EOF'
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.done
        status: done
        owner_testable: true
        tested_at: "2026-08-09"
        title: "Закрыта после подтверждения"
        summary: "обязана пройти гейт"
EOF
    python3 -c "$DEVMAP_VALIDATOR" "$T/ok.yaml" >/dev/null \
        || { echo "САМОТЕСТ ПРОВАЛЕН: done с tested_at покраснел, а не должен"; exit 1; }
    # Больная фикстура: ключ checklist задан дважды — YAML молча оставит
    # последний, дашборд потеряет пункты. Случай пережит 20.08.2026 (pr.движок).
    cat > "$T/dup.yaml" <<'EOF'
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.dup
        status: plan
        title: "Два чек-листа в одной задаче"
        summary: "обязана дать красный: первый список исчезает молча"
        checklist:
          - { label: "Этап 1", done: false }
        volume: { loc: 1, complexity: 1, tokens_k: 1, estimated: true }
        checklist:
          - { label: "Этап 2", done: false }
EOF
    if OUT=$(python3 -c "$DEVMAP_VALIDATOR" "$T/dup.yaml" 2>&1); then
        echo "САМОТЕСТ ПРОВАЛЕН: дубль ключа прошёл гейт (пункты карты теряются молча)"
        echo "$OUT"; exit 1
    fi
    echo "$OUT" | grep -q 'задан дважды' \
        || { echo "САМОТЕСТ ПРОВАЛЕН: красный не про дубль ключа:"; echo "$OUT"; exit 1; }
    # БОЛЬНОЙ СЛУЧАЙ 13.09.2026: поле «глубина» проверялось, но проба на него
    # не была написана вовсе — критерий приёмки фазы 1 держался на слове
    # (ревью кода, I-1). Регресс в этой логике самотест не покрасил бы.
    cat > "$T/глубина-битая.yaml" <<'EOF'
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.глубина
        status: plan
        title: "Глубина с выдуманными значениями"
        summary: "обязана дать красный"
        глубина: { уровень: несуществующий, характер: выдуманный, кем: смена, когда: "2026-09-13" }
EOF
    if OUT=$(python3 -c "$DEVMAP_VALIDATOR" "$T/глубина-битая.yaml" 2>&1); then
        echo "САМОТЕСТ ПРОВАЛЕН: выдуманные уровень и характер прошли гейт"
        echo "$OUT"; exit 1
    fi
    echo "$OUT" | grep -q 'глубина.уровень'         || { echo "САМОТЕСТ ПРОВАЛЕН: красный не про уровень глубины:"; echo "$OUT"; exit 1; }
    cat > "$T/глубина-верная.yaml" <<'EOF'
version: 1
queue: []
layers:
  - { id: infra, label: "Инфра", color: "#5aa06a" }
lines:
  - id: core
    layer: infra
    name: "Ядро"
    tasks:
      - id: t.глубина
        status: plan
        title: "Глубина по правилам"
        summary: "обязана пройти"
        глубина: { уровень: обычная, характер: починка, кем: смена, когда: "2026-09-13" }
EOF
    OUT=$(python3 -c "$DEVMAP_VALIDATOR" "$T/глубина-верная.yaml" 2>&1)         || { echo "САМОТЕСТ ПРОВАЛЕН: верная глубина не прошла:"; echo "$OUT"; exit 1; }
    echo "САМОТЕСТ ПРОЙДЕН: done без tested_at — красный с текстом И-4, done с tested_at — зелёный, «принято» без команды — красный, без даты — красный, с обоими — зелёный, дубль ключа — красный, глубина с выдуманными значениями — красный, верная — зелёная"
    exit 0
fi

# Самотест третьего состояния стоит здесь, а не наверху: он ЗОВЁТ гейт(),
# а функция определена строкой выше. Проверка обязана создавать своё условие,
# а не совпадать с машиной.
# Журнал прогона: SHA + вердикт, и на успех, и на провал. Пишется jq —
# ручная склейка JSON ломается на первой кавычке (улика session-warden).
journal() {  # вердикт, имя-гейта
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    jq -cn --arg ts "$(date -Is)" --arg sha "$HEAD_SHA" --arg v "$1" --arg g "${2:-}" \
        --arg n "$(printf '%s' "$CHANGED" | grep -c . || true)" \
        '{ts:$ts, gate:"pre-commit", head:$sha, staged:($n|tonumber), verdict:$v, failed:$g}' \
        >> "$GATES_LOG" 2>/dev/null || echo "[pre-commit] журнал $GATES_LOG недоступен (гейт не затронут)"
}

fail() {
    echo "[pre-commit] GATE FAIL: $1" >&2
    journal fail "$1"
    exit 1
}

# Третье состояние гейта — жёлтое: находки показываются целиком, коммит идёт.
# Носитель — код возврата 77 (тот же, что в воротах). Без него обкатываемый
# гейт обязан выходить нулём и молчать, а краснящий зря — отключают
# (улика 10.09.2026: четыре находки нового гейта оказались ложными).
YELLOW_CODE=77
гейт() {  # имя, команда... — красный валит коммит, жёлтый только показывает
    local name="$1"; shift
    local out rc=0
    # Код берётся через `|| rc=$?`, а не отдельной строкой: под set -euo
    # присваивание out=$(…) с ненулевым кодом убивает хук ДО того, как
    # что-то напечатано, и жёлтый гейт останавливает коммит молча.
    out=$("$@" 2>&1) || rc=$?
    if [ "$rc" = 0 ]; then
        printf '%s\n' "$out"
        return 0
    fi
    printf '%s\n' "$out"
    if [ "$rc" = "$YELLOW_CODE" ]; then
        echo "[pre-commit] жёлтый гейт $name: находки выше, коммит не остановлен"
        journal yellow "$name"
        return 0
    fi
    fail "$name"
}

if [ "${1:-}" = "--selftest-yellow" ]; then
    # Журналу гейта нужны список staged и SHA — в самотесте их нет.
    CHANGED=""
    HEAD_SHA="selftest"
    OUT=$(гейт "проба-жёлтая" bash -c 'echo "находка обкатки"; exit 77' 2>&1)
    RC=$?
    printf '%s\n' "$OUT" | grep -q "находка обкатки" \
        || { echo "САМОТЕСТ ПРОВАЛЕН: находка жёлтого гейта не показана"; exit 1; }
    [ "$RC" = 0 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: жёлтый гейт остановил коммит"; exit 1; }
    # БОЛЬНОЙ СЛУЧАЙ: красный обязан валить — иначе жёлтое состояние проглотит всё.
    RC_RED=0
    ( гейт "проба-красная" bash -c 'echo сломалось; exit 2' >/dev/null 2>&1 ) || RC_RED=$?
    [ "$RC_RED" = 1 ] \
        || { echo "САМОТЕСТ ПРОВАЛЕН: красный гейт коммит не остановил (код $RC_RED)"; exit 1; }
    # БОЛЬНОЙ СЛУЧАЙ: боевой контекст. Коммит зовёт гейт ПРЯМО, а обе пробы
    # выше звали его внутри $(…) — в субоболочке. Разница смертельна: под
    # set -e присваивание out=$(…) с ненулевым кодом убивало весь хук молча,
    # и жёлтый гейт останавливал КАЖДЫЙ коммит без единой строки объяснения
    # (живой случай 12.09.2026: гейт пайплайна пожелтел — коммит не прошёл).
    гейт "проба-жёлтая-прямо" bash -c 'echo "прямая находка"; exit 77' >/dev/null 2>&1
    echo "САМОТЕСТ ПРОЙДЕН: 4 пути (жёлтый показан и пропущен, прямой вызов пережит, красный валит)"
    exit 0
fi

cd "$(git rev-parse --show-toplevel)"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# core.quotepath=false: кириллические имена («память/») иначе приходят
# экранированными и ломают дальнейшие git-команды по имени файла.
CHANGED=$(git -c core.quotepath=false diff --cached --name-only --diff-filter=ACM)
HEAD_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "initial")

# Временные копии staged-содержимого (К-4): проверяется то, что коммитится, —
# индекс, не рабочее дерево. Имя сохраняет расширение (shellcheck смотрит на него).
STAGE_TMP=$(mktemp -d)
trap 'rm -rf "$STAGE_TMP"' EXIT
staged_copy() {  # staged-файл → печатает путь копии содержимого ИЗ ИНДЕКСА
    local f="$1" out="$STAGE_TMP/${f//\//__}"
    git -c core.quotepath=false show ":$f" > "$out" || return 1
    printf '%s\n' "$out"
}

echo "[pre-commit] staged:"
echo "$CHANGED" | sed 's/^/  /'


# ── 1. Секреты по staged-содержимому ────────────────────────────────────────
"$SCRIPT_DIR/check-secrets.sh" --staged "$PWD" || fail "secrets"

# ── 2. Гигиена контекста: STATE.md, единственность памяти, @-include ────────
python3 "$SCRIPT_DIR/check-context-hygiene.py" --repo "$PWD" || fail "context_hygiene"

# ── 2б. Подробности карточки не раздувают карту ─────────────────────────────
# Карта грузится в контекст каждой сессии; длинный summary читают один раз при
# разборе задачи, а платят за него на каждом шаге.
гейт "прополка_карты" python3 "$SCRIPT_DIR/propolka-karty.py" --проверить

# ── 2в. Писатель карты берёт общий лок ──────────────────────────────────────
гейт "писатели_карты" python3 "$SCRIPT_DIR/check-pisateli-karty.py"

# ── 3. Синтаксис staged-скриптов — по содержимому индекса (К-4) ─────────────
while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
        *.sh)
            SC=$(staged_copy "$f") || fail "index_read:$f"
            if command -v shellcheck >/dev/null 2>&1; then
                shellcheck -S error "$SC" || fail "shellcheck:$f"
            else
                # Честно вслух: bash -n ловит только синтаксис, не стиль.
                echo "[pre-commit] shellcheck не установлен — только bash -n для $f"
                bash -n "$SC" || fail "bash_syntax:$f"
            fi
            ;;
        *.py)
            SC=$(staged_copy "$f") || fail "index_read:$f"
            python3 -m py_compile "$SC" || fail "py_syntax:$f"
            ;;
    esac
done <<< "$CHANGED"

# ── 3б. Кириллические имена переменных в staged-скриптах ────────────────────
# bash такие имена НЕ берёт и печатает в ошибку САМО ЗНАЧЕНИЕ. Сторож команд
# держит это правило с 12.08.2026, но видит только НАБРАННЫЕ команды: файл,
# записанный редактором, шёл мимо. 09.09.2026 так дважды за вечер, и во второй
# раз в лог уехала строка подключения с паролем. Гейт закрывает второй путь.
python3 "$SCRIPT_DIR/check-kirillica-v-imenah.py" <<< "$CHANGED" || fail "кириллица_в_именах"

# ── 3в. Имена НОВЫХ файлов кода — латиницей (задача владельца 11.09) ────────
# Судятся только добавляемые файлы: перевод 601 существующего трогает прод и
# идёт отдельной задачей hr.имена-файлов-латиницей. Здесь — стоп росту.
# Статусы A и R: git mv отдаёт R, и без него переименование в латиницу или
# ОБРАТНО в кириллицу прошло бы мимо гейта (находка I-3 ревью спеки 11.09).
ADDED=$(git -c core.quotepath=false diff --cached --name-only --diff-filter=AR)
гейт "имена_файлов" bash -c 'python3 "$0" <<< "$1"' \
    "$SCRIPT_DIR/check-file-names.py" "$ADDED"

# ── 3г. Середина пайплайна: у крупной задачи есть спека и ревью спеки ───────
# Первый пункт владельца 11.09: «Задачу крупного тира не коммитить без файла
# спеки, привязанного к её id». Уровень считает замер коммита, не моя заявка.
гейт "пайплайн" python3 "$SCRIPT_DIR/check-pipeline.py"

# ── 3д. Тесты до кода: новый модуль приходит вместе с тестом ────────────────
# Второй пункт владельца 11.09. Пока жёлтый: правило новое, база ненулевая
# (75 коммитов истории), а краснящий с первого дня гейт отключают.
гейт "тесты_до_кода" python3 "$SCRIPT_DIR/check-tests-first.py"

# ── 3е. Импорт ведёт в существующий модуль ─────────────────────────────────
# Перевод имён файлов на латиницу оставил 171 импорт продукта указывать на
# исчезнувшее имя (формы «from .модуль import» и «from пакет.модуль import»).
# Линтер этого не видит — он проверяет имена, а не существование модуля.
гейт "импорты" python3 "$SCRIPT_DIR/check-imports.py"

# ── 3е2. Конфиг читают общим загрузчиком, а не голым source ────────────────
# Голый `source` конфига делает файл СТАРШЕ окружения: проба, задающая ключ
# в окружении, играет на боевом. Так 12.09.2026 «/model fable» ушла в рабочую
# панель агента — сессия сменила модель посреди работы.
гейт "загрузка_конфига" python3 "$SCRIPT_DIR/check-konf-zagruzka.py"

# ── 3е3. Функция оболочки определена выше своего вызова ────────────────────
# bash читает файл сверху вниз: вызов выше определения уходит в PATH, печатает
# «command not found» и возвращает пусто. shellcheck такого правила не имеет.
гейт "функция_до_определения" python3 "$SCRIPT_DIR/check-funkciya-do-opredeleniya.py"

# ── 3е-2. Скил не отстал от своей главы ─────────────────────────────────────
# Скил — дословная копия куска главы, а генератор зовут КОМАНДОЙ. Правка главы
# без перегенерации оставляет агента работать по устаревшему правилу, и он
# уверен, что прав. Гейт зовётся только когда в коммите есть глава или скил:
# сборка читает 860 КБ глав, и гонять её на каждом коммите незачем.
if printf '%s\n' "$CHANGED" | grep -qE '^(UNIFIED/|harness/skills/|harness/SBORKA-SKILOV\.py)'; then
    гейт "скилы_от_глав" python3 "$SCRIPT_DIR/../harness/SBORKA-SKILOV.py" --сверить
fi

# ── 3ж. Расписания и юниты зовут существующие файлы ─────────────────────────
# Переименование файла в репозитории ломает вызов в /etc/cron.d и systemd
# молча: гейты судят репозиторий, а расписания живут вне его.
гейт "внешние_вызовы" python3 "$SCRIPT_DIR/check-vneshnie-vyzovy.py"

# ── 3з. Имя продукта не просачивается в харнес ──────────────────────────────
# Судится СПИСОК КОММИТА, а не всё дерево: гейт ворот проверяет целиком, а
# здесь важно не пустить новое вхождение (владелец 12.09.2026).
printf '%s\n' "$CHANGED" | python3 "$SCRIPT_DIR/check-harnes-chist.py" --список \
    || fail "харнес_не_чист"

# ── 3в. Задача не исчезает из карты молча ───────────────────────────────
# 09.09.2026 жадное регулярное выражение при сжатии поля progress съело 88
# задач. YAML остался валидным, все ворота — зелёными, пропажу поймала
# только ручная проверка числа id. Перенос в архив законен, бесследная
# пропажа — потеря данных (И-1).
python3 "$SCRIPT_DIR/check-karta-ne-teryaet-zadachi.py" <<< "$CHANGED" || fail "карта_теряет_задачи"

# ── 4. Схема dev-map.yaml, если карта в коммите ─────────────────────────────
# Проверяются: обязательные поля, допустимые статусы, статусный гейт И-4
# (done только с tested_at либо owner_testable: false), queue без закрытых
# задач. Содержимое — из индекса (К-4), как и весь остальной staged.
if echo "$CHANGED" | grep -qx "dev-map.yaml"; then
    git -c core.quotepath=false show ":dev-map.yaml" > "$STAGE_TMP/dev-map.yaml" \
        || fail "index_read:dev-map.yaml"
    python3 -c "$DEVMAP_VALIDATOR" "$STAGE_TMP/dev-map.yaml" || fail "devmap_schema"
fi

# ── 5. Линтеры кода продукта — данными, не кодом ────────────────────────────
# Гоняются, только когда тронут САМ продукт. Владелец 11.09.2026: «давай теперь
# разделим проект харнеса и продукт <…> чтобы продукт не мешал». Замер: линтер и
# тесты продукта шли на КАЖДОМ коммите харнеса — полторы-пять секунд и целый
# каталог зависимостей ради правки, которая продукта не касается.
# Каталог продукта — ДАННЫЕ (PRODUCT_DIR в harness.conf): у другого проекта он
# называется иначе, а «app» в коде харнеса — чужое имя в общем инструменте.
PRODUKT_TRONUT=0
if [ -n "${PRODUCT_DIR:-}" ]; then
    printf '%s\n' "$CHANGED" | grep -q "^${PRODUCT_DIR}/" && PRODUKT_TRONUT=1
else
    PRODUKT_TRONUT=1      # каталог не назван — судим как раньше, всё подряд
fi
if [ -n "${PRODUCT_LINT_CMD:-}" ] && [ "$PRODUKT_TRONUT" = 0 ]; then
    echo "[pre-commit] линтер продукта пропущен: файлов ${PRODUCT_DIR}/ в коммите нет"
elif [ -n "${PRODUCT_LINT_CMD:-}" ]; then
    echo "[pre-commit] линтер продукта: $PRODUCT_LINT_CMD"
    bash -c "$PRODUCT_LINT_CMD" || fail "product_lint"
else
    # Не молча: пустой ключ — честная строка, чтобы отсутствие линтера было
    # видно в каждом прогоне, а не выяснилось через месяц.
    echo "[pre-commit] язык продукта не задан — линтер добавится с первой задачей (PRODUCT_LINT_CMD в harness.conf)"
fi

# ── 6. Гейты установленных возможностей — данными, не кодом ─────────────────
# Крупный набор (мобильная разработка и подобные) может принести свой гейт
# файлом гейты/*.py. Запускаются ТОЛЬКО у установленных возможностей: пока
# набора нет, ни одного лишнего гейта в коммите не появляется.
# Список staged-файлов уходит гейту на stdin — он сам решает, применим ли он.
CAPS_DIR="${PROJECT_DIR:-$PWD}/harness/возможности"
caps_gates=0
if [ -d "$CAPS_DIR" ]; then
    for pack in "$CAPS_DIR"/*/; do
        [ -f "${pack}.установлено" ] || continue
        [ -d "${pack}гейты" ] || continue
        for gate in "${pack}гейты"/*.py; do
            [ -f "$gate" ] || continue
            caps_gates=$((caps_gates + 1))
            echo "[pre-commit] гейт возможности $(basename "$pack"): $(basename "$gate")"
            printf '%s\n' "$CHANGED" | python3 "$gate" || fail "$(basename "$pack")/$(basename "$gate" .py)"
        done
    done
fi
[ "$caps_gates" = 0 ] && echo "[pre-commit] установленных возможностей со своими гейтами нет"

# ── 7. Пакет-источник не расходится с проектом ──────────────────────────────
# Раскладка копирует ПАКЕТ → ПРОЕКТ: правка, сделанная только в $PROJECT_DIR,
# умрёт при следующем razlozhit-harnes.sh — молча. Улика 11.08.2026: четыре
# починки приёмки синхронизированы агентом ПО ПАМЯТИ. Правило на внимании —
# правило без носителя, поэтому здесь код.
printf '%s\n' "$CHANGED" | python3 "$SCRIPT_DIR/sverit-s-paketom.py" || fail "package_sync"

journal pass ""
echo "[pre-commit] все гейты пройдены"
exit 0
