#!/usr/bin/env bash
# Проба model-for.sh: неизвестный или пустой вид работы — ОТКАЗ, а не умолчание.
#
# Беда (3.5 отчёта соседней установки, их 7f91711): `model-for.sh ревью` вернул
# haiku МОЛЧА — вид в таблице зовётся `ревью_кода`, и опечатка в виде работы
# опустила ревью на самую дешёвую модель. Отчёт другой установки 25.10 (пункт 3,
# карточка отчёта другой установки): `model-for.sh code` так же молча отдал haiku.
# Правило: неизвестный вид — код 1, на stdout пусто, в stderr имя вида и
# список допустимых видов ИЗ ТАБЛИЦЫ; пустой вид — такой же отказ. Звонящие
# демоны не падают: model_dlya на отказе берёт умолчание сама (случаи VG-04).
# Третье: каждый вид, который РЕАЛЬНО зовут вызывающие, есть в боевой таблице —
# список видов берётся разбором вызывающих, а не копией в пробе.
#
# Стенд: своя таблица, свой LOG_DIR; конфиги — несуществующие пути стенда,
# чтобы боевой harness.conf не подмешал DEMON_MODEL или LOG_DIR.
#
# Дверь для мутаций: MODEL_FOR_UNDER_TEST — путь к испорченной копии;
# MODELS_LIB_UNDER_TEST — испорченная копия scripts/lib/models.sh (model_dlya).
#
#   bash scripts/test_model_for.sh
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SUBJECT="${MODEL_FOR_UNDER_TEST:-$HERE/model-for.sh}"
[ -f "$SUBJECT" ] || { echo "ПРОБА СЛОМАНА: нет $SUBJECT"; exit 1; }
MODELS_LIB="${MODELS_LIB_UNDER_TEST:-$HERE/lib/models.sh}"
[ -f "$MODELS_LIB" ] || { echo "ПРОБА СЛОМАНА: нет $MODELS_LIB"; exit 1; }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/log"
cat > "$T/models.yaml" <<'YAML'
модели:
  спека: opus
  ревью_кода: opus
  код: sonnet
  дежурный: haiku
  вид_только_со_стенда: opus
умолчание: haiku
YAML
STAND_KINDS="спека ревью_кода код дежурный вид_только_со_стенда"

bad=0
total=0
verdict() {  # $1 = 1 (верно) / 0, $2 = имя, $3 = подробность
    total=$((total + 1))
    if [ "$1" = 1 ]; then
        echo "  ок    $2"
    else
        echo "  ПЛОХО $2${3:+ — $3}"
        bad=$((bad + 1))
    fi
}

# Прогон: stdout → $T/out, stderr → $T/err, код → $T/rc.
call() {  # $1 = вид работы
    local rc=0
    (cd "$T" && HARNESS_INSTALL_CONF="$T/нет-install.conf" HARNESS_CONF="$T/нет-harness.conf" \
        MODELS_CONF="$T/models.yaml" LOG_DIR="$T/log" DEMON_MODEL= PROJECT_DIR="$T" \
        timeout 30 bash "$SUBJECT" "$1" > "$T/out" 2> "$T/err") || rc=$?
    printf '%s' "$rc" > "$T/rc"
}

# ── БОЛЬНОЙ СЛУЧАЙ: вида нет в таблице ─────────────────────────────────────
call ревью
# Целым словом: в списке видов таблицы стоит «ревью_кода», и подстрока «ревью»
# нашлась бы в нём, даже если вид в строке отказа не назван вовсе.
verdict "$(LC_ALL=C.UTF-8 grep -qw 'ревью' "$T/err" && echo 1 || echo 0)" \
    "БОЛЬНОЙ 3.5: вид «ревью» не из таблицы — назван в stderr" \
    "stderr: «$(cat "$T/err")»"
verdict "$([ "$(cat "$T/rc")" = 1 ] && [ ! -s "$T/out" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ п.3: неизвестный вид «ревью» — отказ с кодом 1, на stdout модели нет" \
    "код $(cat "$T/rc"), stdout «$(tr -d '\n' < "$T/out")»"

# Список допустимых видов — из ТАБЛИЦЫ: «вид_только_со_стенда» есть лишь в
# таблице стенда, вшитый в код список его не назовёт.
lists_all() {  # 0 — в $T/err названы все виды таблицы стенда
    local kind
    for kind in $STAND_KINDS; do
        grep -q -- "$kind" "$T/err" || return 1
    done
}
verdict "$(lists_all && echo 1 || echo 0)" \
    "отказ на неизвестном виде перечисляет все виды таблицы ($STAND_KINDS)" \
    "stderr: «$(cat "$T/err")»"

# ── БОЛЬНОЙ СЛУЧАЙ отчёта другой установки: английское «code» — не «код» ──────────────
# Алиасов нет: «code» обязан получить отказ, а не sonnet и не умолчание.
call code
verdict "$([ "$(cat "$T/rc")" = 1 ] && [ ! -s "$T/out" ] && grep -q 'code' "$T/err" && lists_all && echo 1 || echo 0)" \
    "БОЛЬНОЙ чужой отчёт п.3: вид «code» — код 1, stdout пуст, вид и список видов в stderr" \
    "код $(cat "$T/rc"), stdout «$(tr -d '\n' < "$T/out")», stderr «$(cat "$T/err")»"

# ── пустой вид — тот же отказ ─────────────────────────────────────────────
# До правки пустой вид печатал таблицу на STDOUT: `$(model-for.sh "")` отдал бы
# звонящему таблицу вместо имени модели.
call ""
verdict "$([ "$(cat "$T/rc")" = 1 ] && [ ! -s "$T/out" ] && lists_all && echo 1 || echo 0)" \
    "пустой вид — код 1, stdout пуст, список видов в stderr" \
    "код $(cat "$T/rc"), stdout «$(head -c 200 "$T/out" | tr '\n' ' ')», stderr «$(head -c 300 "$T/err" | tr '\n' ' ')»"

# ── сосед: свежая установка без таблицы ───────────────────────────────────
# Таблицы ещё нет, а демоны уже зовут: судить вид не по чему — умолчание и код 0.
rc=0
(cd "$T" && HARNESS_INSTALL_CONF="$T/нет-install.conf" HARNESS_CONF="$T/нет-harness.conf" \
    MODELS_CONF="$T/нет-таблицы.yaml" LOG_DIR="$T/log" DEMON_MODEL= PROJECT_DIR="$T" \
    timeout 30 bash "$SUBJECT" спека > "$T/out" 2> "$T/err") || rc=$?
verdict "$([ "$rc" = 0 ] && [ "$(tr -d '\n' < "$T/out")" = haiku ] && echo 1 || echo 0)" \
    "таблицы нет вовсе — умолчание haiku и код 0 (свежая установка)" \
    "код $rc, stdout «$(tr -d '\n' < "$T/out")»"

# ── сосед: известный вид молчит в stderr ───────────────────────────────────
call ревью_кода
verdict "$([ ! -s "$T/err" ] && [ "$(tr -d '\n' < "$T/out")" = opus ] && echo 1 || echo 0)" \
    "известный вид «ревью_кода» — opus, stderr пуст" \
    "stdout «$(tr -d '\n' < "$T/out")», stderr «$(cat "$T/err")»"

# ── сосед: строка в stderr несёт ИМЕННО вид, а не общую фразу ──────────────
call совсем-другое-дело
verdict "$(grep -q 'совсем-другое-дело' "$T/err" && echo 1 || echo 0)" \
    "второй неизвестный вид назван своим именем" "stderr: «$(cat "$T/err")»"

# ── VG-04, БОЛЬНОЙ СЛУЧАЙ: жалоба доходит по пути ДЕМОНОВ ─────────────────
# Демоны зовут не model-for.sh, а model_dlya из scripts/lib/models.sh, и та
# глушила stderr: строка о неизвестном виде не доходила ни до журнала демона
# (его stderr), ни до $LOG_DIR/model-dlya.log. Стенд — проект из трёх файлов
# (model-for.sh, lib/config.sh, lib/models.sh): model_dlya ищет model-for.sh
# по PROJECT_DIR, и боевой проект в зов попасть не должен.
mkdir -p "$T/proj/scripts/lib"
cp "$SUBJECT" "$T/proj/scripts/model-for.sh"
cp "$HERE/lib/config.sh" "$T/proj/scripts/lib/config.sh"
cp "$MODELS_LIB" "$T/proj/scripts/lib/models.sh"
rm -f "$T/log/model-dlya.log"
(cd "$T" && HARNESS_INSTALL_CONF="$T/нет-install.conf" HARNESS_CONF="$T/нет-harness.conf" \
    MODELS_CONF="$T/models.yaml" LOG_DIR="$T/log" DEMON_MODEL= PROJECT_DIR="$T/proj" \
    timeout 30 bash -c 'source "$PROJECT_DIR/scripts/lib/models.sh"; model_dlya вид-которого-нет' \
    > "$T/out" 2> "$T/daemon.log")
# Носителей два, и судится каждый: при «или» запись model-for.sh в
# model-dlya.log прятала заглушённый stderr model_dlya.
verdict "$(grep -q 'вид-которого-нет' "$T/daemon.log" 2>/dev/null && echo 1 || echo 0)" \
    "БОЛЬНОЙ VG-04: model_dlya с неизвестным видом — вид назван в stderr демона (его журнал)" \
    "stderr демона «$(cat "$T/daemon.log")»"
verdict "$(grep -q 'вид-которого-нет' "$T/log/model-dlya.log" 2>/dev/null && echo 1 || echo 0)" \
    "БОЛЬНОЙ VG-04: model_dlya с неизвестным видом — вид назван в model-dlya.log" \
    "model-dlya.log «$(cat "$T/log/model-dlya.log" 2>/dev/null)»"
verdict "$([ "$(tr -d '\n' < "$T/out")" = haiku ] && echo 1 || echo 0)" \
    "VG-04: model_dlya на неизвестном виде отдаёт умолчание haiku — вызов claude не падает" \
    "stdout «$(tr -d '\n' < "$T/out")»"
# Сосед: известный вид по пути демонов журнал не засоряет.
rm -f "$T/log/model-dlya.log"
(cd "$T" && HARNESS_INSTALL_CONF="$T/нет-install.conf" HARNESS_CONF="$T/нет-harness.conf" \
    MODELS_CONF="$T/models.yaml" LOG_DIR="$T/log" DEMON_MODEL= PROJECT_DIR="$T/proj" \
    timeout 30 bash -c 'source "$PROJECT_DIR/scripts/lib/models.sh"; model_dlya ревью_кода' \
    > "$T/out" 2> "$T/daemon.log")
verdict "$([ "$(tr -d '\n' < "$T/out")" = opus ] && [ ! -s "$T/daemon.log" ] && [ ! -s "$T/log/model-dlya.log" ] && echo 1 || echo 0)" \
    "VG-04, сосед: известный вид через model_dlya — opus, журнал пуст" \
    "stdout «$(tr -d '\n' < "$T/out")», stderr «$(cat "$T/daemon.log")», model-dlya.log «$(cat "$T/log/model-dlya.log" 2>/dev/null)»"

# ── БОЛЬНОЙ СЛУЧАЙ чужого отчёта п.2: смена поднимается без --model ───────────────
# Отчёт 25.10: AGENT_START_CMD без --model, модель смены бралась из
# ~/.claude/settings.json («opus»), а замысел — sonnet; носителя не было.
# Судится komanda_agenta — одна дверь обоих путей подъёма (start-agent.sh и
# start_agent сторожа). Модель стенда «claude-stend-9» есть только в таблице
# стенда: вшитое в код имя её не даст. Поведение — подставной claude печатает
# свои аргументы и видит ли он носитель чужого входа.
KONF_LIB="${KONF_LIB_UNDER_TEST:-$HERE/lib/config.sh}"
[ -f "$KONF_LIB" ] || { echo "ПРОБА СЛОМАНА: нет $KONF_LIB"; exit 1; }
SM="$T/sm"
mkdir -p "$SM/scripts/lib" "$SM/harness/config" "$T/bin"
cp "$SUBJECT" "$SM/scripts/model-for.sh"
cp "$KONF_LIB" "$SM/scripts/lib/config.sh"
cp "$MODELS_LIB" "$SM/scripts/lib/models.sh"
cat > "$SM/harness/config/models.yaml" <<'YAML'
модели:
  смена: claude-stend-9
  код: sonnet
умолчание: haiku
YAML
cat > "$T/bin/claude" <<'SH'
#!/usr/bin/env bash
printf 'args=%s|token=%s\n' "$*" "${CLAUDE_CODE_OAUTH_TOKEN:-нет}"
SH
chmod +x "$T/bin/claude"
# Подставной носитель на месте настоящего (§4.4, пути
# 1 и 2): смена обязана подниматься через claude-run.sh, иначе встаёт без
# личности. Голый claude в PATH остаётся — по нему видно, что обёртки не было.
cat > "$SM/scripts/claude-run.sh" <<'SH'
#!/usr/bin/env bash
printf 'носитель: %s|token=%s\n' "$*" "${CLAUDE_CODE_OAUTH_TOKEN:-нет}"
SH
# Обёртка аккаунта стоит перед носителем: подставная только передаёт управление.
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$SM/scripts/as-account.sh"
CARRIER_CMD="bash $SM/scripts/claude-run.sh --класс оркестратор"
WRAPPED_CMD="bash $SM/scripts/as-account.sh $CARRIER_CMD"

start_cmd() {  # $1 = AGENT_START_CMD, $2 = путь таблицы → текст komanda_agenta в $T/out, stderr в $T/err
    (cd "$T" && HARNESS_INSTALL_CONF="$T/нет-install.conf" HARNESS_CONF="$T/нет-harness.conf" \
        LOG_DIR="$T/log" DEMON_MODEL= PROJECT_DIR="$SM" MODELS_CONF="$2" \
        timeout 30 bash -c 'source "$1"; komanda_agenta "$2"' _ "$SM/scripts/lib/config.sh" "$1" \
        > "$T/out" 2> "$T/err") || true
}
run_start() {  # $1 = AGENT_START_CMD → что увидел подставной носитель (или голый claude)
    start_cmd "$1" "$SM/harness/config/models.yaml"
    (cd "$T" && PATH="$T/bin:$PATH" CLAUDE_CODE_OAUTH_TOKEN=чужой-ключ timeout 30 bash -c "$(cat "$T/out")") 2>&1
}

seen=$(run_start "claude --dangerously-skip-permissions")
verdict "$([ "$seen" = "носитель: --класс оркестратор --model claude-stend-9 --dangerously-skip-permissions|token=нет" ] && echo 1 || echo 0)" \
    "БОЛЬНОЙ чужой отчёт п.2: подъём без --model — носитель класса «оркестратор» получил модель смены из таблицы, носитель входа сорван" \
    "команда «$(cat "$T/out")», увидел «$seen»"

seen=$(run_start "$T/bin/claude --dangerously-skip-permissions")
verdict "$([ "$seen" = "носитель: --класс оркестратор --model claude-stend-9 --dangerously-skip-permissions|token=нет" ] && echo 1 || echo 0)" \
    "claude полным путём — тоже через носитель, модель смены вставлена" \
    "команда «$(cat "$T/out")», увидел «$seen»"

# Н-1 контракта: обёртка не зависит от --model — команда с --model владельца
# тоже идёт через носитель.
seen=$(run_start "claude --model opus --dangerously-skip-permissions")
verdict "$([ "$seen" = "носитель: --класс оркестратор --model opus --dangerously-skip-permissions|token=нет" ] && echo 1 || echo 0)" \
    "--model владельца в команде — через носитель, --model цел, второго нет" \
    "команда «$(cat "$T/out")», увидел «$seen»"

seen=$(run_start "claude --dangerously-skip-permissions --model=opus")
verdict "$([ "$seen" = "носитель: --класс оркестратор --dangerously-skip-permissions --model=opus|token=нет" ] && echo 1 || echo 0)" \
    "--model=opus (через знак равенства) — через носитель, цел" \
    "команда «$(cat "$T/out")», увидел «$seen»"

# K13: текст команды целиком — префикс срыва входа остаётся, носитель за ним.
start_cmd "claude --model x --dangerously-skip-permissions" "$SM/harness/config/models.yaml"
verdict "$([ "$(cat "$T/out")" = "env -u CLAUDE_CODE_OAUTH_TOKEN $WRAPPED_CMD --model x --dangerously-skip-permissions" ] && echo 1 || echo 0)" \
    "K13: «claude --model x …» → env -u CLAUDE_CODE_OAUTH_TOKEN bash <корень>/scripts/as-account.sh bash <корень>/scripts/claude-run.sh --класс оркестратор --model x …" \
    "команда «$(cat "$T/out")»"

# K15: уже обёрнутая команда не заворачивается второй раз и модель не получает.
start_cmd "$CARRIER_CMD --model x" "$SM/harness/config/models.yaml"
verdict "$([ "$(cat "$T/out")" = "env -u CLAUDE_CODE_OAUTH_TOKEN $CARRIER_CMD --model x" ] && [ "$(grep -o 'claude-run\.sh' "$T/out" | wc -l)" = 1 ] && echo 1 || echo 0)" \
    "K15: команда уже через claude-run.sh — носитель ровно один раз, команда не тронута" \
    "команда «$(cat "$T/out")»"

start_cmd "echo VSTAL" "$SM/harness/config/models.yaml"
verdict "$([ "$(cat "$T/out")" = "env -u CLAUDE_CODE_OAUTH_TOKEN echo VSTAL" ] && echo 1 || echo 0)" \
    "сосед: команда не claude (стенд сторожа) — без носителя и без --model" \
    "команда «$(cat "$T/out")»"

start_cmd "echo hi" "$SM/harness/config/models.yaml"
verdict "$([ "$(cat "$T/out")" = "env -u CLAUDE_CODE_OAUTH_TOKEN echo hi" ] && echo 1 || echo 0)" \
    "K16: «echo hi» → ровно env -u CLAUDE_CODE_OAUTH_TOKEN echo hi" \
    "команда «$(cat "$T/out")»"

printf 'модели:\n  код: sonnet\nумолчание: haiku\n' > "$T/без-смены.yaml"
start_cmd "claude --dangerously-skip-permissions" "$T/без-смены.yaml"
verdict "$([ "$(cat "$T/out")" = "env -u CLAUDE_CODE_OAUTH_TOKEN $WRAPPED_CMD --dangerously-skip-permissions" ] && grep -q 'смена' "$T/err" && echo 1 || echo 0)" \
    "сосед: вида «смена» в таблице нет — через носитель без --model, отказ model-for виден в stderr" \
    "команда «$(cat "$T/out")», stderr «$(cat "$T/err")»"

start_cmd "claude --dangerously-skip-permissions" "$T/нет-таблицы.yaml"
verdict "$([ "$(cat "$T/out")" = "env -u CLAUDE_CODE_OAUTH_TOKEN $WRAPPED_CMD --dangerously-skip-permissions" ] && echo 1 || echo 0)" \
    "сосед: таблицы нет — через носитель без --model, а не смена на умолчании haiku" \
    "команда «$(cat "$T/out")»"

# ── каждый вид, который зовут вызывающие, есть в БОЕВОЙ таблице ───────────
# Виды берутся РАЗБОРОМ вызывающих: копия списка в пробе отстала бы от кода
# молча. Источник, давший ноль видов, — провал пробы: пустой список зелен без
# работы. Пути двух раскладок: проект (scripts/, harness/) и пакет
# (harness/scripts/, harness/hooks/).
HERE="$HERE" python3 - > "$T/callers" 2>&1 <<'PY'
import os, re, sys
from pathlib import Path
import yaml

here = Path(os.environ["HERE"])
def first(*cands):
    for c in cands:
        if c.exists():
            return c
    return None

cfg = first(here.parent / "harness" / "config", here.parent / "config")
demons = first(here.parent / "harness" / "demons", here.parent / "demons")
skill_log = first(here / "hooks" / "skill_log.sh", here.parent / "hooks" / "skill_log.sh")
ask = here / "ask-the-model.py"
lib = here / "lib" / "models.sh"
if not (cfg and demons and skill_log and ask.exists() and lib.exists()):
    print(f"ПРОБА СЛОМАНА: не найдены вызывающие (config={cfg}, demons={demons}, "
          f"skill_log={skill_log}, ask={ask.exists()}, lib={lib.exists()})")
    sys.exit(1)

table = set((yaml.safe_load((cfg / "models.yaml").read_text(encoding="utf-8")) or {})
            .get("модели") or {})
if not table:
    print("ПРОБА СЛОМАНА: боевая таблица моделей пуста")
    sys.exit(1)

sources = {}
# 1. Демоны и скрипты: $(model_dlya <вид>). Пробы (test_*) — стенды, не вызывающие.
calls = set()
for f in sorted(list(demons.glob("*.sh")) + list(here.glob("*.sh")) + list(here.glob("*.py"))):
    if f.name.startswith("test_"):
        continue
    calls |= set(re.findall(r"model_dlya\s+([^\s)\"'$]+)", f.read_text(encoding="utf-8", errors="replace")))
sources["model_dlya в демонах и скриптах"] = calls
# 2. Умолчание самой model_dlya.
sources["умолчание model_dlya"] = set(re.findall(r'kind="\$\{1:-([^}]+)\}"', lib.read_text(encoding="utf-8")))
# 3. Хук скилов: карта «скил → вид».
text = skill_log.read_text(encoding="utf-8")
m = re.search(r"вид_работы\(\)(.*?)esac", text, re.S)
sources["skill_log.sh вид_работы"] = set(re.findall(r'echo "([^"]+)"', m.group(1))) if m else set()
# 4. Страж моделей: виды артефактов.
arts = (yaml.safe_load((cfg / "model-artifacts.yaml").read_text(encoding="utf-8")) or {}).get("артефакты") or {}
sources["model-artifacts.yaml"] = {(v.get("вид") if isinstance(v, dict) else v) for v in arts.values()} - {None, ""}
# 5. Вход модели из кода: виды поводов и умолчание ask-the-model.py.
reasons = (yaml.safe_load((cfg / "model-call-reasons.yaml").read_text(encoding="utf-8")) or {}).get("поводы") or []
sources["model-call-reasons.yaml"] = {r.get("вид") for r in reasons if isinstance(r, dict)} - {None, ""}
sources["умолчание ask-the-model.py"] = set(re.findall(
    r'"--вид",\s*(?:запись\.get\("вид",\s*)?"([^"]+)"', ask.read_text(encoding="utf-8")))
# 6. Подъём смены: вид, который komanda_agenta спрашивает у model-for.sh.
sources["komanda_agenta (подъём смены)"] = set(re.findall(
    r"model-for\.sh\"?\s+([^\s)\"'$|;&]+)", (here / "lib" / "config.sh").read_text(encoding="utf-8")))

bad = 0
for name, kinds in sources.items():
    if not kinds:
        print(f"ПЛОХО-ИСТОЧНИК {name}: разбор дал ноль видов")
        bad += 1
        continue
    missing = sorted(kinds - table)
    print(f"{'ПЛОХО' if missing else 'ок'}-ИСТОЧНИК {name}: видов {len(kinds)}"
          + (f", нет в таблице: {', '.join(missing)}" if missing else ""))
    bad += bool(missing)
sys.exit(1 if bad else 0)
PY
callers_rc=$?
verdict "$([ "$callers_rc" = 0 ] && echo 1 || echo 0)" \
    "все виды, которые зовут вызывающие, есть в боевой таблице (разбор вызывающих)" \
    "$(grep -v '^ок-' "$T/callers" | tr '\n' ';')"
sed 's/^/      /' "$T/callers"

echo
echo "всего путей: $total, неудач: $bad"
[ "$bad" = 0 ]
